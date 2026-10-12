"""The web server: the wall, the viewer's data, and the small copies behind them.

Design constraints that shaped this file:

  * Opening the address shows the Visible wall, with no sign-in: that is the
    point of it. Everything under Hidden/ needs the admin password, and that
    check is made here on every request, including every media file: knowing
    a hidden file's address is not enough to fetch it.
  * Media addresses contain a fingerprint of the content and the recipe
    version, and are served `immutable` for a year. Over a 275 ms link the
    cheapest request is the one never made: anything the phone has seen once
    costs nothing the second time.
  * Originals are never served. There is no route here that reads from the
    library of originals at all; this server only opens the small copies.
  * Files arrive two ways, and both need the admin password: through the
    import door on its own port (dav.py), for the machine this runs on, and
    from the admin page itself, in pieces (uploads.py), for a phone or laptop.
    Nothing else here adds, changes or removes a photo or video.
  * No JavaScript build step and no CDN: the page asks nobody else for anything.
"""

from __future__ import annotations

import gzip
import json
import os
import re
import shutil
import signal
import threading
import time
import urllib.parse

from flask import Flask, abort, jsonify, make_response, render_template, request, send_file

from . import PIPELINE, __version__, auth, config, dav, library, media, places, uploads
from .worker import worker

app = Flask(__name__)
app.json.sort_keys = False
app.config["TEMPLATES_AUTO_RELOAD"] = True      # one page; a stat per request is nothing
# Nothing this server is sent is larger than a password or a tag, with one
# exception: a piece of a file the admin is uploading from the page, and the
# route that takes those raises its own ceiling once it knows who is asking.
MAX_BODY = 64 * 1024
app.config["MAX_CONTENT_LENGTH"] = MAX_BODY

# What the one page may load: its own files, and the tiny blurred placeholders
# that travel inside the feed as data. Nothing from anywhere else, no inline
# script, and no other site may put this one in a frame.
CSP = ("default-src 'self'; img-src 'self' data:; media-src 'self' blob:; "
       "object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")

YEAR = 31536000
_KEY = re.compile(r"^[0-9a-f]{24}$")
_PHOTO = re.compile(r"^[tml]\.webp$")
_PREVIEW = "p.mp4"
_LIVE = "live.mp4"                   # the moving half of a Live Photo
_RUNG = re.compile(r"^v/(\d{3,4})\.(m3u8|m4s)$")


# --------------------------------------------------------------- who is it --

def role() -> str | None:
    """'admin', 'viewer' or None.

    With no viewer password set, everyone who can reach the address is a
    viewer without signing in. That is the normal arrangement.
    """
    proved = auth.role_of(request.cookies.get(auth.COOKIE))
    if proved:
        return proved
    if auth.is_configured() and not auth.has_viewer():
        return "viewer"
    return None


def need(minimum: str):
    """Decorator: refuse unless the visitor has at least this role."""
    def wrap(fn):
        def inner(*args, **kwargs):
            have = role()
            if have is None:
                return jsonify(error="Please sign in."), 401
            if minimum == "admin" and have != "admin":
                return jsonify(error="That needs the admin password."), 403
            # Anything that changes state must come from our own page. A custom
            # header cannot be sent by another site's form or image tag.
            if request.method != "GET" and request.headers.get("X-Waterfall") != "1":
                return jsonify(error="Bad request."), 400
            return fn(*args, **kwargs)
        inner.__name__ = fn.__name__
        return inner
    return wrap


def _realm() -> str:
    """Which root a request is about. Asking for the hidden one without the
    admin password is refused here, once, for every route that takes it."""
    realm = request.args.get("realm", "visible")
    if realm not in config.REALMS:
        abort(400)
    if realm == "hidden" and role() != "admin":
        abort(403)
    return realm


def _set_cookie(response, token: str | None):
    if token is None:
        response.delete_cookie(auth.COOKIE)
    else:
        # `secure` follows the request: over plain http (a home network, a VPN)
        # a Secure cookie would never be sent back. Behind a proxy that adds
        # https, set WATERFALL_TRUSTED_PROXY so this server knows it.
        response.set_cookie(auth.COOKIE, token, max_age=auth.MAX_AGE, httponly=True,
                            samesite="Lax", secure=request.is_secure)
    return response


# -------------------------------------------------------------------- shell --

def _asset_version() -> str:
    """Changes whenever a static file does, so a phone never runs stale code."""
    newest = 0.0
    static = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
    for name in os.listdir(static):
        newest = max(newest, os.path.getmtime(os.path.join(static, name)))
    return f"{__version__}-{int(newest)}"


@app.get("/")
def index():
    response = make_response(render_template("index.html", v=_asset_version()))
    response.headers["Cache-Control"] = "no-cache"
    return response


@app.get("/favicon.ico")
def favicon():
    """There is no icon. Saying so plainly keeps the one thing a browser asks
    for unprompted out of its list of errors."""
    response = make_response("", 204)
    response.headers["Cache-Control"] = f"public, max-age={YEAR}"
    return response


@app.get("/healthz")
def healthz():
    """Proves the index opens and answers, not merely that a process is up.

    Anyone can ask, so it says how much is in the library only to the admin:
    the counts cover both walls, and how much is on the Hidden one is nobody
    else's to know. Nor does a failure explain itself to a stranger.
    """
    try:
        c = library.counts()
        found = {"ok": True, "version": __version__}
        if role() == "admin":
            found.update(ready=c["ready"], waiting=c["waiting"])
        return jsonify(found)
    except Exception:
        return jsonify(ok=False), 500


@app.get("/api/me")
def me():
    have = role()
    return jsonify(configured=auth.is_configured(), role=have, admin=have == "admin",
                   has_viewer=auth.has_viewer(), version=__version__, pipeline=PIPELINE,
                   simulated=config.SIMULATE_LINK or None, admin_idle=config.ADMIN_IDLE,
                   admin_left=auth.admin_seconds_left(request.cookies.get(auth.COOKIE)))


@app.post("/api/setup")
def setup():
    """First visit only: choose the admin password. Refused forever after."""
    if auth.is_configured():
        return jsonify(error="Waterfall is already set up."), 409
    if request.headers.get("X-Waterfall") != "1":
        return jsonify(error="Bad request."), 400
    body = request.get_json(silent=True) or {}
    try:
        auth.set_passwords(str(body.get("admin") or ""), None)
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    return _set_cookie(jsonify(role="admin"), auth.issue("admin"))


@app.post("/api/login")
def login():
    if request.headers.get("X-Waterfall") != "1":
        return jsonify(error="Bad request."), 400
    address = request.remote_addr or "?"
    wait = auth.throttled(address)
    if wait:
        return jsonify(error=f"Too many wrong passwords. Try again in {wait} seconds."), 429
    body = request.get_json(silent=True) or {}
    found = auth.check(str(body.get("password") or ""), address)
    if not found:
        return jsonify(error="That password is not right."), 401
    return _set_cookie(jsonify(role=found), auth.issue(found))


@app.post("/api/logout")
def logout():
    if request.headers.get("X-Waterfall") != "1":
        return jsonify(error="Bad request."), 400
    return _set_cookie(jsonify(ok=True), None)


# --------------------------------------------------------------------- feed --

@app.get("/api/sections")
@need("viewer")
def api_sections():
    realm = _realm()
    return jsonify(sections=library.sections(realm), totals=library.totals(realm),
                   tags=library.tag_list(realm))


@app.get("/api/feed")
@need("viewer")
def api_feed():
    args = request.args
    limit = max(1, min(args.get("limit", 40, type=int), 100))
    items = library.feed(_realm(), args.get("section", type=int), args.get("order", "shuffle"),
                         args.get("seed", 1, type=int), max(0, args.get("offset", 0, type=int)),
                         limit, args.get("kind", "all"), args.get("tag") or None)
    return jsonify(items=items, more=len(items) == limit)


@app.get("/api/item/<int:item_id>")
@need("viewer")
def api_item(item_id: int):
    found = library.item(item_id, allow_hidden=role() == "admin")
    if not found:
        return jsonify(error="Not found."), 404
    meta = found.get("meta") or {}
    if "lat" in meta:
        # "City, Country", from the list of towns that ships with Waterfall.
        # No map service is asked; the position never leaves this machine.
        found["place"] = places.nearest(meta["lat"], meta["lon"])
    if role() == "admin":
        found["suggest"] = library.tag_suggestions(found["realm"])
    return jsonify(found)


# -------------------------------------------------------------------- media --

def _master_playlist(info: dict, minimum: int, bandwidth: int) -> str:
    """The list of video sizes offered to this phone, for this viewing.

    This is where "minimum quality" and "match the link speed" both live:

      * sizes below the chosen minimum are simply not offered, so the player
        waits for a better one rather than dropping to a worse one;
      * the size listed FIRST is the one the player starts on, so it is chosen
        from the speed the phone has just measured instead of always starting
        at the bottom and climbing.

    After that the phone's own player keeps measuring and switches by itself.
    """
    rungs = info.get("rungs") or []
    offered = [r for r in rungs if min(r["w"], r["h"]) >= minimum] or rungs[-1:]
    fits = [r for r in offered if r["peak"] <= bandwidth * 0.7]
    first = fits[-1] if fits else offered[0]
    ordered = [first] + [r for r in offered if r is not first]
    lines = ["#EXTM3U", "#EXT-X-VERSION:7", "#EXT-X-INDEPENDENT-SEGMENTS"]
    for r in ordered:
        lines.append(f'#EXT-X-STREAM-INF:BANDWIDTH={max(r["peak"], r["avg"])},'
                     f'AVERAGE-BANDWIDTH={r["avg"]},RESOLUTION={r["w"]}x{r["h"]},'
                     f'CODECS="{r["codecs"]}"')
        lines.append(f'{r["name"]}.m3u8')
    return "\n".join(lines) + "\n"


@app.get("/m/<int:pipeline>/<key>/<path:name>")
def media_file(pipeline: int, key: str, name: str):
    have = role()
    if have is None:
        abort(401)
    if pipeline != PIPELINE or not _KEY.match(key):
        abort(404)
    row = library.find_ready_by_key(key, allow_hidden=have == "admin")
    if row is None:
        abort(404)

    if name == "v/master.m3u8":
        text = _master_playlist(json.loads(row["info"] or "{}"),
                                request.args.get("min", 0, type=int),
                                request.args.get("bw", 2_000_000, type=int))
        response = make_response(text)
        response.headers["Content-Type"] = "application/vnd.apple.mpegurl"
        response.headers["Cache-Control"] = "private, max-age=3600"
        return response

    if _PHOTO.match(name):
        mimetype = "image/webp"
    elif name in (_PREVIEW, _LIVE):
        mimetype = "video/mp4"
    elif _RUNG.match(name):
        mimetype = "application/vnd.apple.mpegurl" if name.endswith(".m3u8") else "video/mp4"
    else:
        abort(404)
    path = os.path.join(media.deriv_dir(key), name)
    if not os.path.isfile(path):
        abort(404)
    # conditional=True is what makes byte-range requests work, and a video
    # player on an iPhone will not play anything served without them.
    response = send_file(path, mimetype=mimetype, conditional=True)
    response.headers["Cache-Control"] = f"private, max-age={YEAR}, immutable"
    return response


# -------------------------------------------------------------------- admin --

@app.get("/api/admin/status")
@need("admin")
def admin_status():
    c = library.counts()
    w = worker.snapshot()
    eta = None
    if w.get("video_bytes_per_second") and c["video_bytes_waiting"]:
        eta = int(c["video_bytes_waiting"] / w["video_bytes_per_second"])
    try:
        disk = shutil.disk_usage(config.DATA_DIR)
        storage = {"free": disk.free, "total": disk.total, "copies": c["copy_bytes"],
                   "awaiting": c["original_bytes"]}
    except OSError:
        storage = None
    return jsonify(counts=c, worker=w, eta_seconds=eta, failures=library.failures(),
                   storage=storage, import_port=config.DAV_PORT,
                   hardware=media.hardware_report(), keeps_originals=config.KEEP_ORIGINALS,
                   upload_piece=uploads.PIECE,
                   tools={"ffmpeg": bool(config.FFMPEG), "exiftool": bool(config.EXIFTOOL),
                          "heic": media.HEIC_OK})


@app.post("/api/admin/rescan")
@need("admin")
def admin_rescan():
    worker.rescan_now()
    return jsonify(ok=True)


@app.post("/api/admin/retry")
@need("admin")
def admin_retry():
    n = library.retry_failed()
    worker.rescan_now()
    return jsonify(ok=True, retried=n)


@app.post("/api/admin/touch")
@need("admin")
def admin_touch():
    """The admin is still here: the page says so when it is touched, and the
    sign-in is renewed. Nothing else renews it, so a page left open, or one
    quietly asking for the library's status, lapses like any other."""
    return _set_cookie(jsonify(ok=True, admin_left=config.ADMIN_IDLE), auth.issue("admin"))


@app.post("/api/admin/upload-ticket")
@need("admin")
def admin_upload_ticket():
    """Leave to go on uploading after the admin's sign-in has lapsed."""
    return jsonify(ticket=auth.issue_ticket())


@app.post("/api/admin/upload")
def admin_upload():
    """One piece of one file, sent by the admin page from a phone or a laptop.
    The pieces are put together, and the rules enforced, in uploads.py. It is
    open to the admin, and to a ticket the admin's page was given when the
    upload began."""
    if request.headers.get("X-Waterfall") != "1":
        return jsonify(error="Bad request."), 400
    if role() != "admin" and not auth.ticket_ok(request.headers.get("X-Upload-Ticket")):
        return jsonify(error="That needs the admin password."), 403
    request.max_content_length = uploads.PIECE
    head = request.headers
    try:
        result = uploads.receive(
            request.args.get("realm", ""), urllib.parse.unquote(head.get("X-Upload-Path", "")),
            head.get("X-Upload-Id", ""), int(head.get("X-Upload-Offset", "-1")),
            int(head.get("X-Upload-Total", "0")), request.get_data(cache=False),
            float(head.get("X-Upload-Modified") or 0) or None)
    except uploads.Refused as no:
        return jsonify(error=no.message), no.status
    except ValueError:
        return jsonify(error="Bad request."), 400
    except OSError as exc:
        return jsonify(error=exc.strerror or "The file could not be kept."), 507 if exc.errno == 28 else 500
    if result["done"] and not result.get("skipped"):
        worker.rescan_soon()
    return jsonify(result)


@app.post("/api/admin/tag")
@need("admin")
def admin_tag():
    """Add or remove a tag on one item. A tag puts the item in another
    category as well as its own folder; the file is not copied or touched."""
    body = request.get_json(silent=True) or {}
    try:
        now = library.set_tag(int(body.get("id") or 0), body.get("tag"), bool(body.get("on")))
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    if now is None:
        return jsonify(error="Not found."), 404
    return jsonify(tags=now)


@app.post("/api/admin/passwords")
@need("admin")
def admin_passwords():
    body = request.get_json(silent=True) or {}
    try:
        auth.set_passwords(str(body["admin"]) if body.get("admin") else None,
                           str(body["viewer"]) if body.get("viewer") else None,
                           clear_viewer=bool(body.get("clear_viewer")))
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    # Every old session just stopped working, including this one: renew it.
    return _set_cookie(jsonify(ok=True, has_viewer=auth.has_viewer()), auth.issue("admin"))


# ------------------------------------------------------------------ plumbing --

@app.after_request
def finish(response):
    # Compress the feed and the page's own code; never media (already compressed).
    kind = response.mimetype or ""
    if (response.status_code == 200 and not response.direct_passthrough
            and (kind.startswith("text/") or kind in ("application/json", "application/javascript"))
            and "gzip" in request.headers.get("Accept-Encoding", "")
            and "Content-Encoding" not in response.headers):
        body = response.get_data()
        if len(body) > 800:
            response.set_data(gzip.compress(body, 6))
            response.headers["Content-Encoding"] = "gzip"
            response.headers.add("Vary", "Accept-Encoding")
    if request.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Frame-Options"] = "DENY"
    if kind == "text/html":
        response.headers["Content-Security-Policy"] = CSP
    return response


class SlowLink:
    """Development only: make localhost feel like a slow link a long way off.

    Every request waits one round trip before its first byte, and all responses
    share one bandwidth budget, as they would share one satellite uplink. It is
    an approximation (it does not model TCP slow start), but it is enough to
    tell whether preloading and size selection are doing their jobs.
    """

    def __init__(self, inner, delay_ms: float, mb_per_s: float):
        self.inner, self.delay, self.rate = inner, delay_ms / 1000, mb_per_s * 1_000_000
        self.lock, self.free_at = threading.Lock(), 0.0

    def _spend(self, n: int) -> None:
        with self.lock:
            start = max(time.time(), self.free_at)
            self.free_at = start + n / self.rate
            wait = self.free_at - time.time()
        if wait > 0:
            time.sleep(wait)

    def __call__(self, environ, start_response):
        time.sleep(self.delay)
        body = self.inner(environ, start_response)

        def paced():
            try:
                for chunk in body:
                    for i in range(0, len(chunk), 16384):
                        piece = chunk[i:i + 16384]
                        self._spend(len(piece))
                        yield piece
            finally:
                close = getattr(body, "close", None)
                if close:
                    close()
        return paced()


def serve_options() -> dict:
    """How the web server is started. Kept apart from main() so it can be tested."""
    options = {"host": config.BIND, "port": config.PORT, "ident": "waterfall",
               # A phone opens up to six connections; a few more threads than
               # that keeps thumbnails flowing while a video segment is sent.
               "threads": 12,
               # Refused from the request's own headers, before any of an
               # oversized body has been read or kept. The ceiling is one piece
               # of an upload: the server reads a whole request before it can
               # ask who sent it, so this much can be sent by anyone, and the
               # number of connections is what bounds the total.
               "max_request_body_size": uploads.PIECE + MAX_BODY,
               "connection_limit": 48}
    if config.TRUSTED_PROXY:
        # Behind a reverse proxy every visitor arrives from the proxy's address
        # and over plain http. Believing the proxy's word on who it is passing
        # on, and how they reached it, keeps the wrong-password limit per
        # visitor and marks the sign-in cookie https-only. The same headers
        # from anyone else are thrown away.
        options.update(trusted_proxy=config.TRUSTED_PROXY, trusted_proxy_count=1,
                       trusted_proxy_headers={"x-forwarded-for", "x-forwarded-proto"},
                       clear_untrusted_proxy_headers=True)
    return options


def _allow_open_files(wanted: int = 4096) -> None:
    """The import door may hold several hundred connections waiting their
    turn, and each is an open file. Raise this process's own limit as far as
    it is allowed to, so that a bulk copy cannot run it out."""
    try:
        import resource
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        target = wanted if hard == resource.RLIM_INFINITY else min(wanted, hard)
        if soft != resource.RLIM_INFINITY and soft < target:
            resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
    except (ImportError, ValueError, OSError):
        pass


def main() -> None:
    from waitress import serve
    _allow_open_files()
    config.ensure_roots()
    library.connect()
    requeued = library.requeue_after_recipe_change()
    if requeued:
        print(f"  recipe changed: {requeued} items will be brought up to date", flush=True)
    auth.seed_from_environment()

    def shut_down(signum, _frame):
        # `docker stop` sends SIGTERM. Take the running conversion down with us;
        # its item stays queued and is redone cleanly on the next start.
        worker.stop()
        media.stop_conversions()
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, shut_down)
    signal.signal(signal.SIGINT, shut_down)
    # WATERFALL_WORKER=0 serves an existing library without converting anything
    # or opening the import door: for a second development instance sharing
    # one data folder.
    if os.environ.get("WATERFALL_WORKER", "1") != "0":
        worker.start()
        dav.serve(on_change=worker.rescan_soon)
        print(f"  import door on port {config.DAV_PORT} ({config.DAV_BIND})", flush=True)
    wsgi = app
    if config.SIMULATE_LINK:
        delay, rate = (float(x) for x in config.SIMULATE_LINK.split(","))
        wsgi = SlowLink(app, delay, rate)
        print(f"  SIMULATED LINK: {delay:.0f} ms, {rate} MB/s -- development only", flush=True)
    print(f"Waterfall {__version__} on port {config.PORT}  data={config.DATA_DIR}", flush=True)
    serve(wsgi, **serve_options())


if __name__ == "__main__":
    main()
