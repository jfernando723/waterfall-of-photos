"""The import door: how photos and videos get into Waterfall when the container
has no folder from the machine it runs on.

A NAS's file manager can usually attach a remote folder and copy to it the
same way it copies between two of its own (on a Synology: File Station, Tools,
Remote Connection). This module is what it connects to: a small WebDAV server
on its own port, opened only by the admin password. The copy happens on the
NAS itself, NAS to container, so nothing travels over the slow link to wherever
you are, and the container never sees a share. Any WebDAV client will do.

What a connected client sees is fixed:

    /Visible/   anyone who opens Waterfall sees what is put here
    /Hidden/    only the admin password shows what is put here

Those two folders cannot be deleted, renamed or added to at the top level.
Everything inside them is the client's to arrange: folders become sections.

Why this is its own port and its own plain-Python server, not more Flask
routes: a 4K clip is several gigabytes, and the web server used for the
interface reads a whole request into a buffer before handing it over. Here the
bytes go from the socket straight to disk.

A file only appears under its real name once it has arrived completely. While
it is uploading it has a hidden temporary name, which the scanner ignores, so
half a video is never converted and never shown.
"""

from __future__ import annotations

import base64
import collections
import mimetypes
import os
import re
import secrets
import shutil
import sys
import threading
import time
import urllib.parse
import uuid
from email.utils import formatdate
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from xml.sax.saxutils import escape

from . import auth, config

CHUNK = 1 << 20
SPARE = 512 * 1024 * 1024       # refuse an upload that would leave less than this free
SMALL = 4 * CHUNK               # the most that is read from someone who has not shown the password
# Connections served at once. Each is a thread, and with no ceiling anything
# that could reach this port could open them until the container's process
# limit was spent and the converter could no longer start. A file manager
# copying folders uses a handful.
MAX_CLIENTS = 32
# Connections beyond those wait their turn instead of being turned away: a
# waiting connection is a socket and nothing else, no thread. Together that is
# five hundred at once, so a bulk copy of several hundred files started in one
# go is served in order with none refused. Past that, the door hangs up.
MAX_WAITING = 468
WAIT_LIMIT = 300                # seconds: one that has waited this long has given up anyway
ALLOW = "OPTIONS, PROPFIND, GET, HEAD, PUT, MKCOL, DELETE, MOVE, COPY, LOCK, UNLOCK, PROPPATCH"

_on_change = lambda: None       # set by serve(): "something arrived, look again soon"


def _say(line: str) -> None:
    """One line in the log, which on a NAS is the only place to look when a
    file manager will not copy something in. Never a file or folder name: the
    log is not behind the admin password."""
    print("import door: " + line, flush=True)


def _plain(text: str, most: int) -> str:
    """Something a client sent, made safe to print."""
    return "".join(c if 32 <= ord(c) < 127 else "?" for c in str(text)[:most])


class Refuse(Exception):
    def __init__(self, status: int, message: str = ""):
        super().__init__(message)
        self.status, self.message = status, message


def _split(url_path: str) -> list[str]:
    """A request path as folder names, or Refuse if it tries to leave the library."""
    raw = urllib.parse.urlsplit(url_path).path
    parts = [urllib.parse.unquote(p, errors="strict") for p in raw.split("/") if p]
    for part in parts:
        if part in (".", "..") or "\x00" in part or "/" in part or "\\" in part:
            raise Refuse(400, "That name is not allowed.")
    if parts and parts[0] not in config.REALMS.values():
        raise Refuse(404, "Only the Visible and Hidden folders exist here.")
    return parts


def _fs(parts: list[str]) -> str:
    path = os.path.join(config.MEDIA_DIR, *parts)
    # Belt and braces: nothing here creates links, but if one were ever present
    # it must not lead a request out of the library.
    root = os.path.realpath(config.MEDIA_DIR)
    real = os.path.realpath(path)
    if real != root and not real.startswith(root + os.sep):
        raise Refuse(403, "Outside the library.")
    return path


def _href(parts: list[str], is_dir: bool) -> str:
    quoted = "/".join(urllib.parse.quote(p) for p in parts)
    return "/" + quoted + ("/" if is_dir and parts else "")


def _props(parts: list[str], path: str) -> str:
    st = os.stat(path)
    is_dir = os.path.isdir(path)
    name = parts[-1] if parts else "Waterfall"
    body = (f"<D:displayname>{escape(name)}</D:displayname>"
            f"<D:getlastmodified>{formatdate(st.st_mtime, usegmt=True)}</D:getlastmodified>"
            f"<D:creationdate>{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(st.st_mtime))}</D:creationdate>"
            "<D:supportedlock><D:lockentry><D:lockscope><D:exclusive/></D:lockscope>"
            "<D:locktype><D:write/></D:locktype></D:lockentry></D:supportedlock>"
            # Whoever holds the admin password may do everything here. Said
            # outright, because a client that asks and is told nothing may
            # conclude it is allowed nothing.
            + _MAY)
    if is_dir:
        # How much room there is, in the form clients ask for it. One that is
        # told nothing may show, or assume, none.
        room = shutil.disk_usage(path)
        body += ("<D:resourcetype><D:collection/></D:resourcetype>"
                 f"<D:quota-available-bytes>{max(0, room.free - SPARE)}</D:quota-available-bytes>"
                 f"<D:quota-used-bytes>{room.used}</D:quota-used-bytes>")
    else:
        kind = mimetypes.guess_type(name)[0] or "application/octet-stream"
        body += ("<D:resourcetype/>"
                 f"<D:getcontentlength>{st.st_size}</D:getcontentlength>"
                 f"<D:getcontenttype>{kind}</D:getcontenttype>"
                 f"<D:getetag>{_etag(st)}</D:getetag>")
    return (f"<D:response><D:href>{escape(_href(parts, is_dir))}</D:href><D:propstat>"
            f"<D:prop>{body}</D:prop><D:status>HTTP/1.1 200 OK</D:status>"
            "</D:propstat></D:response>")


_MAY = ("<D:current-user-privilege-set>" + "".join(
    f"<D:privilege><D:{p}/></D:privilege>" for p in (
        "read", "write", "write-properties", "write-content", "bind", "unbind", "unlock",
        "read-current-user-privilege-set")) + "</D:current-user-privilege-set>")


def _etag(st) -> str:
    return f'"{st.st_size:x}-{st.st_mtime_ns:x}"'


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "Waterfall-import"
    sys_version = ""
    # Five minutes of silence from a client ends its connection, so a copy
    # that stalls does not hold a thread (and a half-written file) for ever.
    timeout = 300

    _why = ""       # why this request was answered as it was, for the log
    _asked = b""    # the start of a small request body, kept only while WATERFALL_DAV_LOG is on

    def log_message(self, *args):       # the admin page is the place for status
        pass

    def log_request(self, code="-", size="-"):
        """Called once for every answer. A refusal is always written to the
        log; an ordinary answer only when WATERFALL_DAV_LOG asks for it. A
        missing file is ordinary (clients ask before they write), and so is
        the first request of a client that waits to be asked for the password."""
        try:
            status = int(code)
        except (TypeError, ValueError):
            return
        headers = getattr(self, "headers", None)
        sent_password = bool(headers and headers.get("Authorization"))
        looking = getattr(self, "command", None) in ("GET", "HEAD", "PROPFIND")
        routine = status < 400 or (status == 404 and looking) or (status == 401 and not sent_password)
        if config.DAV_LOG or not routine:
            _say(self._described(status))

    def _described(self, status: int) -> str:
        """What was asked and what was answered, without any name in it."""
        headers = getattr(self, "headers", None) or {}
        path = urllib.parse.urlsplit(getattr(self, "path", "") or "").path
        bits = [f"{_plain(getattr(self, 'command', None) or '?', 12)} answered {status}"]
        if self._why:
            bits.append(self._why)
        bits.append(f"{len([p for p in path.split('/') if p])} folders deep")
        size = headers.get("Content-Length") or headers.get("X-Expected-Entity-Length")
        if size:
            bits.append(f"{_plain(size, 20)} bytes announced")
        for name in ("Transfer-Encoding", "Expect", "Content-Range", "Depth", "Overwrite",
                     "If-Match", "If-None-Match"):
            if headers.get(name):
                bits.append(f"{name}: {_plain(headers[name], 40)}")
        if self._asked:
            # The names of what was asked for (properties, a lock), never the
            # text between them: that can hold a user name or a file name.
            names = []
            for name in re.findall(r"<(?:[\w.-]+:)?([\w.-]+)", self._asked.decode("latin-1")):
                if name not in names and name not in ("xml", "propfind", "prop"):
                    names.append(name)
            if names:
                bits.append("asks for: " + _plain(" ".join(names), 300))
        if not headers.get("Authorization"):
            bits.append("no password sent")
        if headers.get("User-Agent"):
            bits.append(f"client: {_plain(headers['User-Agent'], 60)}")
        return ", ".join(bits)

    # -- plumbing -------------------------------------------------------------
    def _send(self, status: int, body: bytes = b"", content_type: str = "text/plain; charset=utf-8",
              headers: dict | None = None) -> None:
        self.send_response(status)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        if body or status not in (204, 304):
            self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body and self.command != "HEAD":
            self.wfile.write(body)

    def _chunked(self) -> bool:
        return "chunked" in (self.headers.get("Transfer-Encoding") or "").lower()

    def _length(self, *also: str) -> int:
        """The size the request says its body is. A size below zero is a lie
        with a purpose: handed to read() it would mean "everything you send,
        kept in memory, until you stop"."""
        for name in ("Content-Length",) + also:
            text = (self.headers.get(name) or "").strip()
            if text:
                size = int(text)
                if size < 0:
                    raise ValueError("negative length")
                return size
        return 0

    def _body(self):
        """The request body in pieces, whether sent with a length or chunked."""
        if self._chunked():
            while True:
                line = self.rfile.readline(4096)
                if not line:
                    raise ConnectionError("upload stopped")
                size = int(line.split(b";")[0].strip() or b"0", 16)
                if size < 0:
                    raise ValueError("negative chunk")
                if size == 0:
                    while self.rfile.readline(4096) not in (b"\r\n", b"\n", b""):
                        pass
                    return
                while size:
                    piece = self.rfile.read(min(size, CHUNK))
                    if not piece:
                        raise ConnectionError("upload stopped")
                    size -= len(piece)
                    yield piece
                self.rfile.readline(4096)
        else:
            left = self._length()
            while left:
                piece = self.rfile.read(min(left, CHUNK))
                if not piece:
                    raise ConnectionError("upload stopped")
                left -= len(piece)
                yield piece

    def _drain(self) -> None:
        for piece in self._body():
            if config.DAV_LOG and len(self._asked) < 4096:
                self._asked += piece[:4096 - len(self._asked)]

    def _drain_if_small(self) -> None:
        """Read a small body so that the client hears the answer rather than a
        dropped connection. A large one is not read just to be refused: that
        connection is closed instead."""
        try:
            if self._length() <= SMALL and not self._chunked():
                self._drain()
                return
        except (ValueError, ConnectionError):
            pass
        self.close_connection = True

    def _allowed(self) -> bool:
        """Admin password or nothing. The username is ignored: clients insist
        on asking for one, and Waterfall has none."""
        if not auth.is_configured():
            self._why = "no admin password has been chosen yet"
            self.close_connection = True
            self._send(503, b"Open Waterfall in a browser and choose the admin password first.\n",
                       headers={"Connection": "close"})
            return False
        header = self.headers.get("Authorization") or ""
        password = None
        if header.lower().startswith("basic "):
            try:
                password = base64.b64decode(header[6:]).decode("utf-8").split(":", 1)[1]
            except Exception:
                self._why = "the password sent could not be read"
        elif header:
            self._why = "a kind of sign-in the door does not offer: " + _plain(header.split(" ", 1)[0], 20)
        if password is not None:
            wait = auth.throttled(self.client_address[0])
            if auth.check_admin(password, self.client_address[0]):
                return True
            self._why = (f"too many wrong passwords from this address, {wait} seconds to wait"
                         if wait else "wrong password")
        self._drain_if_small()
        self.close_connection = True
        self._send(401, b"The Waterfall admin password is needed.\n",
                   headers={"WWW-Authenticate": 'Basic realm="Waterfall import"', "Connection": "close"})
        return False

    def _run(self, action) -> None:
        self._why, self._asked = "", b""
        try:
            self._answer(action)
        except (ConnectionError, TimeoutError):
            # The client went away, mid-request or while being answered.
            self.close_connection = True
            _say(f"{_plain(self.command or '?', 12)} ended early, the client went away"
                 + (f" ({self._why})" if self._why else ""))

    def _answer(self, action) -> None:
        try:
            if self.command != "OPTIONS" and not self._allowed():
                return
            action()
        except Refuse as no:
            self.close_connection = True
            self._why = no.message
            self._send(no.status, (no.message + "\n").encode(), headers={"Connection": "close"})
        except (ConnectionError, TimeoutError):
            raise
        except ValueError:
            # A body that does not follow the rules it announced. Nothing was kept.
            self.close_connection = True
            self._why = "the upload did not follow the size it announced"
            self._send(400, b"That upload was malformed.\n", headers={"Connection": "close"})
        except OSError as exc:
            self.close_connection = True
            status = 507 if exc.errno == 28 else 500        # 28: no space left on device
            self._why = exc.strerror or type(exc).__name__
            self._send(status, f"{exc.strerror or exc}\n".encode(), headers={"Connection": "close"})

    def _inside(self, parts: list[str], verb: str) -> None:
        """The two roots are permanent, and nothing may sit beside them."""
        if len(parts) < 2:
            raise Refuse(403, f"You cannot {verb} at the top level. Work inside Visible or Hidden.")

    # -- reading --------------------------------------------------------------
    def do_OPTIONS(self):
        # Answered without the password, so it reads no more than a refusal would.
        self._run(lambda: (self._drain_if_small(), self._send(200, headers={
            "DAV": "1, 2", "Allow": ALLOW, "MS-Author-Via": "DAV"})))

    def do_PROPFIND(self):
        def go():
            self._drain()
            parts = _split(self.path)
            path = _fs(parts)
            if not os.path.exists(path):
                raise Refuse(404, "Not found.")
            out = [_props(parts, path)]
            if os.path.isdir(path) and self.headers.get("Depth", "1") != "0":
                names = sorted(os.listdir(path), key=str.lower)
                if not parts:
                    names = [n for n in config.REALMS.values() if n in names]
                for name in names:
                    if name.startswith("."):
                        continue            # uploads in progress, and system clutter
                    out.append(_props(parts + [name], os.path.join(path, name)))
            xml = ('<?xml version="1.0" encoding="utf-8"?><D:multistatus xmlns:D="DAV:">'
                   + "".join(out) + "</D:multistatus>")
            self._send(207, xml.encode("utf-8"), 'application/xml; charset="utf-8"')
        self._run(go)

    def _get(self):
        self._drain()
        parts = _split(self.path)
        path = _fs(parts)
        if os.path.isdir(path):
            return self._send(200, b"Waterfall import. Connect with a WebDAV client.\n")
        if not os.path.isfile(path):
            raise Refuse(404, "Not found.")
        st = os.stat(path)
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(path)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(st.st_size))
        self.send_header("Last-Modified", formatdate(st.st_mtime, usegmt=True))
        self.send_header("ETag", _etag(st))
        self.end_headers()
        if self.command == "HEAD":
            return
        with open(path, "rb") as f:
            shutil.copyfileobj(f, self.wfile, CHUNK)

    def do_GET(self):
        self._run(self._get)

    def do_HEAD(self):
        self._run(self._get)

    # -- writing --------------------------------------------------------------
    def do_MKCOL(self):
        def go():
            self._drain()
            parts = _split(self.path)
            self._inside(parts, "create a folder")
            path = _fs(parts)
            if os.path.exists(path):
                raise Refuse(405, "That already exists.")
            if not os.path.isdir(os.path.dirname(path)):
                raise Refuse(409, "The folder above it does not exist.")
            os.mkdir(path)
            self._send(201)
        self._run(go)

    def do_PUT(self):
        def go():
            parts = _split(self.path)
            self._inside(parts, "put files")
            path = _fs(parts)
            folder = os.path.dirname(path)
            if not os.path.isdir(folder):
                raise Refuse(409, "The folder above it does not exist.")
            if os.path.isdir(path):
                raise Refuse(405, "That is a folder.")
            expected = self._length("X-Expected-Entity-Length")
            if shutil.disk_usage(folder).free < expected + SPARE:
                raise Refuse(507, "Not enough room left for that file.")
            existed = os.path.exists(path)
            # A leading dot keeps the half-arrived file out of listings and out
            # of the scanner's sight; the rename at the end is atomic.
            temp = os.path.join(folder, f".{parts[-1]}.part-{secrets.token_hex(4)}")
            got = 0
            try:
                with open(temp, "wb") as f:
                    for piece in self._body():
                        f.write(piece)
                        got += len(piece)
                os.replace(temp, path)
            except BaseException:
                self._why = f"{got} of {expected} bytes had arrived" if expected else f"{got} bytes had arrived"
                try:
                    os.remove(temp)
                except OSError:
                    pass
                raise
            self._why = f"{got} bytes kept"
            _on_change()
            self._send(204 if existed else 201, headers={"ETag": _etag(os.stat(path))})
        self._run(go)

    def do_DELETE(self):
        def go():
            self._drain()
            parts = _split(self.path)
            self._inside(parts, "delete")
            path = _fs(parts)
            if os.path.isdir(path):
                shutil.rmtree(path)
            elif os.path.exists(path):
                os.remove(path)
            else:
                raise Refuse(404, "Not found.")
            _on_change()
            self._send(204)
        self._run(go)

    def _transfer(self, move: bool) -> None:
        self._drain()
        parts = _split(self.path)
        self._inside(parts, "move things")
        source = _fs(parts)
        if not os.path.exists(source):
            raise Refuse(404, "Not found.")
        target_parts = _split(self.headers.get("Destination") or "")
        self._inside(target_parts, "put things")
        target = _fs(target_parts)
        if not os.path.isdir(os.path.dirname(target)):
            raise Refuse(409, "The folder above the destination does not exist.")
        if os.path.realpath(target) == os.path.realpath(source):
            raise Refuse(403, "Source and destination are the same.")
        existed = os.path.exists(target)
        if existed:
            if (self.headers.get("Overwrite") or "T").strip().upper() == "F":
                raise Refuse(412, "The destination already exists.")
            shutil.rmtree(target) if os.path.isdir(target) else os.remove(target)
        if move:
            os.replace(source, target)
        elif os.path.isdir(source):
            shutil.copytree(source, target)
        else:
            shutil.copy2(source, target)
        _on_change()
        self._send(204 if existed else 201)

    def do_MOVE(self):
        self._run(lambda: self._transfer(True))

    def do_COPY(self):
        self._run(lambda: self._transfer(False))

    # -- what clients insist on before they will write ------------------------
    def do_LOCK(self):
        def go():
            self._drain()
            parts = _split(self.path)
            path = _fs(parts)
            created = False
            if not os.path.exists(path):
                # A lock on a name that does not exist yet reserves it, which is
                # how some clients begin an upload. The scanner skips empty files.
                self._inside(parts, "put files")
                if not os.path.isdir(os.path.dirname(path)):
                    raise Refuse(409, "The folder above it does not exist.")
                open(path, "ab").close()
                created = True
            token = f"opaquelocktoken:{uuid.uuid4()}"
            xml = ('<?xml version="1.0" encoding="utf-8"?><D:prop xmlns:D="DAV:"><D:lockdiscovery>'
                   "<D:activelock><D:locktype><D:write/></D:locktype><D:lockscope><D:exclusive/>"
                   "</D:lockscope><D:depth>infinity</D:depth><D:timeout>Second-3600</D:timeout>"
                   f"<D:locktoken><D:href>{token}</D:href></D:locktoken>"
                   f"<D:lockroot><D:href>{escape(_href(parts, os.path.isdir(path)))}</D:href></D:lockroot>"
                   "</D:activelock></D:lockdiscovery></D:prop>")
            self._send(201 if created else 200, xml.encode("utf-8"),
                       'application/xml; charset="utf-8"', {"Lock-Token": f"<{token}>"})
        self._run(go)

    def do_UNLOCK(self):
        self._run(lambda: (self._drain(), self._send(204)))

    def do_PROPPATCH(self):
        def go():
            self._drain()
            parts = _split(self.path)
            path = _fs(parts)
            if not os.path.exists(path):
                raise Refuse(404, "Not found.")
            xml = ('<?xml version="1.0" encoding="utf-8"?><D:multistatus xmlns:D="DAV:"><D:response>'
                   f"<D:href>{escape(_href(parts, os.path.isdir(path)))}</D:href><D:propstat><D:prop/>"
                   "<D:status>HTTP/1.1 200 OK</D:status></D:propstat></D:response></D:multistatus>")
            self._send(207, xml.encode("utf-8"), 'application/xml; charset="utf-8"')
        self._run(go)


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 128

    def __init__(self, *args, **kwargs):
        self._turns = threading.Lock()
        self._most, self._most_waiting = MAX_CLIENTS, MAX_WAITING
        self._serving = 0
        self._waiting: collections.deque = collections.deque()
        self._last_full = 0.0
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        with self._turns:
            if self._serving >= self._most:
                if len(self._waiting) < self._most_waiting:
                    self._waiting.append((request, client_address, time.monotonic()))
                    return
                full = True
            else:
                self._serving += 1
                full = False
        if full:
            # Full, and the waiting room too: hang up at once, hold nothing.
            if time.monotonic() - self._last_full > 30:
                self._last_full = time.monotonic()
                _say(f"turned a connection away: {self._most} being served and {self._most_waiting} waiting")
            self.shutdown_request(request)
            return
        if not self._begin(request, client_address):
            self._next()                # it never started: its place is free again

    def _begin(self, request, client_address) -> bool:
        try:
            super().process_request(request, client_address)
            return True
        except Exception:
            self.shutdown_request(request)
            return False

    def _next(self) -> None:
        """A connection has finished. Its place goes to whoever has waited
        longest, or is given back if nobody is waiting."""
        while True:
            with self._turns:
                if not self._waiting:
                    self._serving -= 1
                    return
                request, client_address, since = self._waiting.popleft()
            if time.monotonic() - since > WAIT_LIMIT:
                self.shutdown_request(request)
                continue
            if self._begin(request, client_address):
                return

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._next()

    def handle_error(self, request, client_address):
        # A client that goes away between requests is its own business, not a
        # fault worth a page of traceback in the log.
        if isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError)):
            if config.DAV_LOG:
                _say("a client went away between requests")
            return
        super().handle_error(request, client_address)

    def server_close(self):
        with self._turns:
            waiting, self._waiting = list(self._waiting), collections.deque()
        for request, _, _ in waiting:
            self.shutdown_request(request)
        super().server_close()


def serve(on_change=None, bind: str | None = None, port: int | None = None) -> _Server:
    """Start the import door in the background and return the server."""
    global _on_change
    if on_change:
        _on_change = on_change
    config.ensure_roots()
    server = _Server((bind if bind is not None else config.DAV_BIND,
                      port if port is not None else config.DAV_PORT), Handler)
    threading.Thread(target=server.serve_forever, name="waterfall-import", daemon=True).start()
    return server
