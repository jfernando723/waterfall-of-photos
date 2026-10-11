"""Where things live, and which external tools do the heavy lifting.

Everything is an environment variable with a default that is right inside the
container, so docker-compose.yml stays short enough to paste into a NAS's
container screen (Synology's Container Manager, for one) with nothing to get
wrong.

There is ONE place on disk, /data, a Docker named volume. The container is
given no folder from the machine it runs on, so there is nothing of yours that
it can see, let alone alter. Inside /data:

    media/Visible/   what anyone who opens Waterfall sees
    media/Hidden/    what only the admin password shows
    d/               the small copies made from both
    waterfall.db      the index and the settings

Waterfall keeps the small copies only. Once a file's copies exist and have been
checked, the full-size file that was copied in is removed and a short text file
with the same name plus ".waterfall.txt" is left in its place, so the folders
still show what is in them and can still be rearranged through the import door.

Photos and videos arrive in media/ through the import door (dav.py): File
Station on the NAS connects to it and copies folders in. Which of the two roots
a folder is dropped into decides, once and for all, who can see it.
"""

from __future__ import annotations

import os
import shutil


def _find(tool: str, env: str) -> str | None:
    explicit = os.environ.get(env)
    if explicit:
        return explicit
    return shutil.which(tool)


def _find_ffmpeg() -> str | None:
    found = _find("ffmpeg", "WATERFALL_FFMPEG")
    if found:
        return found
    # Development on a machine with no ffmpeg installed: the imageio-ffmpeg
    # wheel carries a static binary inside the project's own .venv, so nothing
    # has to be installed system-wide. The container uses Debian's ffmpeg.
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


_HERE = os.path.dirname(os.path.abspath(__file__))
_CHECKOUT = os.path.dirname(_HERE)

DATA_DIR = os.environ.get("WATERFALL_DATA") or (
    "/data" if os.path.isdir("/data") else os.path.join(_CHECKOUT, ".devdata"))
MEDIA_DIR = os.environ.get("WATERFALL_MEDIA") or os.path.join(DATA_DIR, "media")

# The two roots. The folder names are what the owner sees in their file
# manager, so they say what they do. Nothing else may exist beside them.
VISIBLE, HIDDEN = "Visible", "Hidden"
REALMS = {"visible": VISIBLE, "hidden": HIDDEN}

PORT = int(os.environ.get("WATERFALL_PORT", "7275"))
# All interfaces inside the container (Docker decides what is published). The
# development script overrides this to 127.0.0.1 unless a phone needs to reach it.
BIND = os.environ.get("WATERFALL_BIND", "0.0.0.0")

# The import door: a second port, speaking WebDAV, admin password only.
# docker-compose.yml publishes it on the NAS's own loopback address, so File
# Station on the NAS can reach it and nothing else on the network can.
DAV_PORT = int(os.environ.get("WATERFALL_DAV_PORT", "7276"))
DAV_BIND = os.environ.get("WATERFALL_DAV_BIND", BIND)

# Only when Waterfall sits behind a reverse proxy (to add https, say): the
# address that proxy connects FROM, as this container sees it, or "*" if the
# container can be reached by nothing else. Empty means no proxy is believed.
TRUSTED_PROXY = os.environ.get("WATERFALL_TRUSTED_PROXY", "").strip()

FFMPEG = _find_ffmpeg()
EXIFTOOL = _find("exiftool", "WATERFALL_EXIFTOOL")

# How hard the background converter may lean on the machine. A NAS has other
# work to do, backups above all; conversion is never urgent.
FFMPEG_THREADS = int(os.environ.get("WATERFALL_FFMPEG_THREADS", "0"))  # 0 = ffmpeg decides
X264_PRESET = os.environ.get("WATERFALL_X264_PRESET", "veryfast")
NICE = int(os.environ.get("WATERFALL_NICE", "10"))

# Development only: make this machine's network behave like a slow, distant
# link, so preloading and quality switching can be judged at a desk. Format is
# "<delay ms>,<MB per second>", e.g. "275,1.5". Empty means off.
SIMULATE_LINK = os.environ.get("WATERFALL_SIMULATE_LINK", "")

# Small copies only: a full-size file is removed once its copies are made and
# checked. Set to 1 to keep the full-size files as well.
KEEP_ORIGINALS = os.environ.get("WATERFALL_KEEP_ORIGINALS", "0") == "1"
# What is left where a full-size file was. The name it had, plus this.
STUB = ".waterfall.txt"

# Video conversion on the machine's video chip: "auto" tries it and falls back
# to software by itself; "off" never tries.
HWACCEL = os.environ.get("WATERFALL_HWACCEL", "auto")
VAAPI_DEVICE = os.environ.get("WATERFALL_VAAPI_DEVICE", "/dev/dri/renderD128")

# Your own place names, added to the list that ships (see places.py): a file in
# the same format as waterfall/data/places-preferred.tsv.
PLACES_FILE = os.environ.get("WATERFALL_PLACES") or os.path.join(DATA_DIR, "places-preferred.tsv")

DB_PATH = os.path.join(DATA_DIR, "waterfall.db")
DERIV_DIR = os.path.join(DATA_DIR, "d")

PHOTO_EXT = {".heic", ".heif", ".jpg", ".jpeg", ".png", ".webp", ".gif", ".tif", ".tiff", ".avif"}
VIDEO_EXT = {".mov", ".mp4", ".m4v", ".mkv", ".avi", ".webm", ".3gp", ".mts"}


def ensure_roots() -> None:
    """Create the two roots. Safe to call whenever; never removes anything."""
    for name in REALMS.values():
        os.makedirs(os.path.join(MEDIA_DIR, name), exist_ok=True)
    os.makedirs(DERIV_DIR, exist_ok=True)
