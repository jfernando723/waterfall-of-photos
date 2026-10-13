"""Uploading from the page itself: the way in for a phone or a laptop.

The import door (dav.py) is for the machine Waterfall runs on, whose file
manager copies folders to it. A phone has no such thing, so the admin page can
send files as well. They arrive on the web port, which reads a whole request
before handing it over, so a file is sent in pieces of a few megabytes and put
together here. That is also what lets a 4 GB video survive a link that drops:
only the piece in flight is sent again.

The rules are the door's rules:

  * only the admin, and only into Visible/ or Hidden/;
  * a file has a hidden name until every byte of it has arrived, so half a
    video is never converted and never shown;
  * nothing is ever overwritten. The same file sent twice is recognised and
    skipped; a different file with a name already taken is kept under a new one.
"""

from __future__ import annotations

import os
import re
import shutil
import threading

from . import config, library

PIECE = 4 * 1024 * 1024         # the most one request may carry
SPARE = 512 * 1024 * 1024       # refuse a file that would leave less than this free
MAX_DEPTH = 12
_ID = re.compile(r"^[0-9a-f]{16}$")
_finishing = threading.Lock()


class Refused(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


def _place(realm: str, rel: str) -> tuple[str, str]:
    """Where a file is to go: (folder on disk, file name). Refuses anything
    that is not a photo or video inside one of the two roots."""
    if realm not in config.REALMS:
        raise Refused(400, "Choose the Visible or the Hidden wall.")
    parts = [p.strip() for p in rel.replace("\\", "/").split("/") if p.strip()]
    if not parts or len(parts) > MAX_DEPTH:
        raise Refused(400, "That file has no name, or sits too many folders deep.")
    for part in parts:
        if (part in (".", "..") or part.startswith(".") or part in library.SKIP_DIRS
                or len(part.encode("utf-8")) > 200 or any(ord(c) < 32 for c in part)):
            raise Refused(400, f"The name {part[:40]!r} cannot be used.")
    if library._kind(parts[-1]) is None:
        raise Refused(415, "Only photos and videos can be added.")
    root = os.path.realpath(os.path.join(config.MEDIA_DIR, config.REALMS[realm]))
    folder = os.path.join(root, *parts[:-1])
    real = os.path.realpath(folder)
    if real != root and not real.startswith(root + os.sep):
        raise Refused(400, "That folder is outside the library.")
    return folder, parts[-1]


def _already_here(folder: str, name: str, total: int) -> bool:
    """The same file, sent before: by name and size, whether the full-size
    file is still here or only the stand-in left when it was converted."""
    try:
        if os.path.getsize(os.path.join(folder, name)) == total:
            return True
    except OSError:
        pass
    stub = library.read_stub(os.path.join(folder, name + config.STUB))
    return bool(stub) and int(stub.get("size") or -1) == total


def _free_name(folder: str, name: str) -> str:
    """This name, or the first variant of it that nothing here is using."""
    stem, ext = os.path.splitext(name)
    candidate, n = name, 1
    while os.path.exists(os.path.join(folder, candidate)) \
            or os.path.exists(os.path.join(folder, candidate + config.STUB)):
        n += 1
        candidate = f"{stem} ({n}){ext}"
    return candidate


def receive(realm: str, rel: str, upload_id: str, offset: int, total: int,
            data: bytes, modified: float | None = None) -> dict:
    """Take one piece of one file. Returns how much of the file has arrived
    and, once it is complete, the name it was kept under."""
    if not _ID.match(upload_id or ""):
        raise Refused(400, "Bad request.")
    if total <= 0 or offset < 0 or len(data) > PIECE or offset + len(data) > total:
        raise Refused(400, "That piece does not fit the file it belongs to.")
    folder, name = _place(realm, rel)
    temp = os.path.join(folder, f".{name}.part-{upload_id}")
    if offset == 0:
        if _already_here(folder, name, total):
            return {"have": total, "done": True, "skipped": True, "name": name}
        os.makedirs(folder, exist_ok=True)
        if shutil.disk_usage(folder).free < total + SPARE:
            raise Refused(507, "There is not enough room left for that file.")
        with open(temp, "wb"):
            pass                                    # a first piece sent again starts the file again
    try:
        have = os.path.getsize(temp)
    except OSError:
        if _already_here(folder, name, total):
            # Its last piece was taken and the file given its name, and the
            # answer saying so never reached the page, which has sent the
            # piece again. That is a finished file, not a fault.
            return {"have": total, "done": True, "skipped": False, "name": name}
        raise Refused(409, "That upload is not known here any more. Start the file again.") from None
    if have != offset:
        # A piece sent twice, or one that never arrived: say where the file
        # really stands, and the page carries on from there.
        return {"have": have, "done": False, "resume": True}
    with open(temp, "ab") as f:
        f.write(data)
        if have + len(data) >= total:
            # The last piece: on the disk before the file is given its name
            # (the import door does the same, and says why).
            f.flush()
            os.fsync(f.fileno())
    have += len(data)
    if have < total:
        return {"have": have, "done": False}
    with _finishing:
        if _already_here(folder, name, total):
            os.remove(temp)
            return {"have": total, "done": True, "skipped": True, "name": name}
        kept = _free_name(folder, name)
        if modified and 0 < modified < 4102444800:  # the date the device gave, for files that carry none
            try:
                os.utime(temp, (modified, modified))
            except OSError:
                pass
        os.replace(temp, os.path.join(folder, kept))
    return {"have": total, "done": True, "skipped": False, "name": kept}
