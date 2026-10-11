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
import mimetypes
import os
import secrets
import shutil
import threading
import time
import urllib.parse
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
ALLOW = "OPTIONS, PROPFIND, GET, HEAD, PUT, MKCOL, DELETE, MOVE, COPY, LOCK, UNLOCK, PROPPATCH"

_on_change = lambda: None       # set by serve(): "something arrived, look again soon"


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
            "<D:locktype><D:write/></D:locktype></D:lockentry></D:supportedlock>")
    if is_dir:
        body += "<D:resourcetype><D:collection/></D:resourcetype>"
    else:
        kind = mimetypes.guess_type(name)[0] or "application/octet-stream"
        body += ("<D:resourcetype/>"
                 f"<D:getcontentlength>{st.st_size}</D:getcontentlength>"
                 f"<D:getcontenttype>{kind}</D:getcontenttype>"
                 f'<D:getetag>"{st.st_size:x}-{st.st_mtime_ns:x}"</D:getetag>')
    return (f"<D:response><D:href>{escape(_href(parts, is_dir))}</D:href><D:propstat>"
            f"<D:prop>{body}</D:prop><D:status>HTTP/1.1 200 OK</D:status>"
            "</D:propstat></D:response>")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "Waterfall-import"
    sys_version = ""
    # Five minutes of silence from a client ends its connection, so a copy
    # that stalls does not hold a thread (and a half-written file) for ever.
    timeout = 300

    def log_message(self, *args):       # the admin page is the place for status
        pass

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
        for _ in self._body():
            pass

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
                password = None
        if password is not None and auth.check_admin(password, self.client_address[0]):
            return True
        self._drain_if_small()
        self.close_connection = True
        self._send(401, b"The Waterfall admin password is needed.\n",
                   headers={"WWW-Authenticate": 'Basic realm="Waterfall import"', "Connection": "close"})
        return False

    def _run(self, action) -> None:
        try:
            if self.command != "OPTIONS" and not self._allowed():
                return
            action()
        except Refuse as no:
            self.close_connection = True
            self._send(no.status, (no.message + "\n").encode(), headers={"Connection": "close"})
        except (ConnectionError, TimeoutError):
            self.close_connection = True
        except ValueError:
            # A body that does not follow the rules it announced. Nothing was kept.
            self.close_connection = True
            self._send(400, b"That upload was malformed.\n", headers={"Connection": "close"})
        except OSError as exc:
            self.close_connection = True
            status = 507 if exc.errno == 28 else 500        # 28: no space left on device
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
        size = os.path.getsize(path)
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(path)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(size))
        self.send_header("Last-Modified", formatdate(os.path.getmtime(path), usegmt=True))
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
            try:
                with open(temp, "wb") as f:
                    for piece in self._body():
                        f.write(piece)
                os.replace(temp, path)
            except BaseException:
                try:
                    os.remove(temp)
                except OSError:
                    pass
                raise
            _on_change()
            self._send(204 if existed else 201)
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
            token = f"opaquelocktoken:{secrets.token_hex(16)}"
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

    def __init__(self, *args, **kwargs):
        self._slots = threading.BoundedSemaphore(MAX_CLIENTS)
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)      # full: hang up at once, hold nothing
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


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
