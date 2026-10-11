"""Turn one original into the small copies a phone can afford to download.

The link this is built for is a slow one a long way off: a satellite connection
on another continent, about 275 ms away and 1 to 2.6 MB per second. A 4 MB phone
photo takes seconds over that, and 4K phone video (3 MB per second and up)
cannot play at all. So nothing here is ever served as-is. Every original becomes:

    photo  ->  t.webp   grid thumbnail, small enough to arrive in ~one round trip
               m.webp   what fills the phone screen
               l.webp   for zooming, or for when you choose to wait
    video  ->  t.webp / m.webp   poster frame
               p.mp4    a few silent seconds that move on the wall
               v/<N>.m3u8 + v/<N>.m4s   one per rung of the quality ladder

The ladder is made ONCE, in advance. Choosing between rungs while watching is
then free, and iPhone Safari does it by itself (HLS adaptive streaming): it
measures the link and switches rung mid-video. Compressing on demand instead
would need the NAS to re-encode 4K in real time, which a small four-core NAS
cannot do in software.

This module only ever reads the original. It has no code that writes, renames
or removes anything outside the output directory it is handed.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from PIL import Image, ImageOps

from . import config

try:  # HEIC is the iPhone's default photo format; Pillow needs this to open it.
    import pillow_heif
    pillow_heif.register_heif_opener()
    HEIC_OK = True
except Exception:  # pragma: no cover - the container always has it
    HEIC_OK = False


class MediaError(Exception):
    """A file could not be read or converted. The message is shown to the admin."""


# For every file that came from outside. A video file can be a list of other
# things to open in disguise (a playlist with a video's name), and ffmpeg would
# follow it. This holds it to the file it was handed: nothing fetched from the
# network, on the say-so of a file somebody was sent.
_FROM_OUTSIDE = ["-protocol_whitelist", "file,crypto,data"]


# Conversions in flight, so that stopping the server stops them too. An ffmpeg
# left running after its parent has gone would keep four cores busy for an hour
# producing files nobody is waiting for.
_running: set = set()


def stop_conversions() -> None:
    for proc in list(_running):
        try:
            proc.kill()
        except OSError:
            pass


# ----------------------------------------------------------------- identity --

def fingerprint(path: str) -> str:
    """A stable name for a file's content, cheap enough for multi-GB videos.

    Size plus the first and last megabyte. Hashing a whole 4K clip would read
    gigabytes off spinning disks just to name it; this reads two megabytes and
    still changes if the file is replaced by a different one. The small copies
    are stored under this name, so moving or renaming an original costs
    nothing, and a phone's cache stays valid.
    """
    size = os.path.getsize(path)
    h = hashlib.sha256(size.to_bytes(8, "big"))
    chunk = 1 << 20
    with open(path, "rb") as f:
        h.update(f.read(chunk))
        if size > 2 * chunk:
            f.seek(size - chunk)
            h.update(f.read(chunk))
    return h.hexdigest()[:24]


def deriv_dir(key: str) -> str:
    return os.path.join(config.DERIV_DIR, key[:2], key)


# ----------------------------------------------------------------- metadata --

_EXIF_FIELDS = [
    "FileType", "MIMEType", "ImageWidth", "ImageHeight", "Rotation", "Orientation",
    "Duration", "VideoFrameRate", "CompressorID", "AvgBitrate", "AudioFormat",
    "SubSecDateTimeOriginal", "DateTimeOriginal", "OffsetTimeOriginal",
    "CreationDate", "CreateDate", "Make", "Model", "LensModel",
    "GPSLatitude", "GPSLongitude", "GPSAltitude",
    "FNumber", "ExposureTime", "ISO", "FocalLength", "FocalLengthIn35mmFormat",
    "ContentIdentifier", "MediaGroupUUID",
]


def probe_many(paths: list[str]) -> dict[str, dict]:
    """Read metadata for a batch of files with one exiftool run.

    exiftool is the one tool that understands both halves of an iPhone library:
    EXIF inside HEIC, and Apple's QuickTime keys inside MOV (the capture time
    WITH its timezone, the GPS fix, the camera model). One process per file
    would cost a Perl start-up each time, so files go in batches.
    """
    if not paths:
        return {}
    if not config.EXIFTOOL:
        raise MediaError("exiftool is not installed")
    cmd = [config.EXIFTOOL, "-json", "-n", "-api", "QuickTimeUTC=1",
           "-api", "LargeFileSupport=1"] + [f"-{f}" for f in _EXIF_FIELDS] + ["--"] + paths
    run = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    try:
        rows = json.loads(run.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise MediaError(f"exiftool gave unreadable output: {exc}") from exc
    return {row["SourceFile"]: row for row in rows}


_DATE = re.compile(
    r"(\d{4}):(\d{2}):(\d{2}) (\d{2}):(\d{2}):(\d{2})(?:\.\d+)?\s*(Z|[+-]\d{2}:?\d{2})?")


def _parse_date(text) -> tuple[str, float] | None:
    """-> (wall-clock time where it was taken, UTC timestamp for sorting)."""
    if not isinstance(text, str):
        return None
    m = _DATE.match(text.strip())
    if not m or m.group(1) == "0000":
        return None
    y, mo, d, h, mi, s = (int(m.group(i)) for i in range(1, 7))
    try:
        local = datetime(y, mo, d, h, mi, s)
    except ValueError:
        return None
    off = m.group(7)
    if off and off != "Z":
        sign = -1 if off[0] == "-" else 1
        digits = off[1:].replace(":", "")
        delta = timedelta(hours=int(digits[:2]), minutes=int(digits[2:4])) * sign
    else:
        # No timezone recorded. Treating the wall-clock time as UTC is wrong by
        # at most half a day, which only ever matters for sort order.
        delta = timedelta(0)
    utc = (local - delta).replace(tzinfo=timezone.utc)
    return local.strftime("%Y-%m-%d %H:%M:%S"), utc.timestamp()


def taken_at(meta: dict, path: str) -> tuple[str, float, str]:
    """The best available capture time, and where it came from."""
    offset = meta.get("OffsetTimeOriginal")
    candidates = [
        ("camera", meta.get("SubSecDateTimeOriginal")),
        ("camera", (meta.get("DateTimeOriginal") or "") + (offset or "")
         if meta.get("DateTimeOriginal") else None),
        ("camera", meta.get("CreationDate")),   # Apple's key: carries the timezone
        ("camera", meta.get("CreateDate")),
    ]
    for source, text in candidates:
        parsed = _parse_date(text)
        if parsed:
            return parsed[0], parsed[1], source
    mtime = os.path.getmtime(path)
    local = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S")
    return local, mtime, "file date"


def describe(meta: dict) -> dict:
    """The human-readable facts for the details panel. Only what is present."""
    out = {}
    make, model = meta.get("Make"), meta.get("Model")
    if model:
        out["camera"] = model if (make and str(model).startswith(str(make))) or not make \
            else f"{make} {model}"
    if meta.get("LensModel"):
        out["lens"] = str(meta["LensModel"])
    lat, lon = meta.get("GPSLatitude"), meta.get("GPSLongitude")
    if isinstance(lat, (int, float)) and isinstance(lon, (int, float)) and (lat or lon):
        out["lat"], out["lon"] = round(lat, 6), round(lon, 6)
        if isinstance(meta.get("GPSAltitude"), (int, float)):
            out["alt"] = round(meta["GPSAltitude"])
    bits = []
    if meta.get("FocalLengthIn35mmFormat"):
        bits.append(f"{round(float(meta['FocalLengthIn35mmFormat']))} mm")
    if meta.get("FNumber"):
        bits.append(f"f/{float(meta['FNumber']):g}")
    exposure = meta.get("ExposureTime")
    if isinstance(exposure, (int, float)) and exposure > 0:
        bits.append(f"1/{round(1 / exposure)} s" if exposure < 1 else f"{exposure:g} s")
    if meta.get("ISO"):
        bits.append(f"ISO {meta['ISO']}")
    if bits:
        out["exposure"] = "  ".join(bits)
    if meta.get("CompressorID"):
        out["codec"] = {"hvc1": "HEVC", "hev1": "HEVC", "avc1": "H.264",
                        "dvh1": "Dolby Vision"}.get(meta["CompressorID"], meta["CompressorID"])
    if meta.get("VideoFrameRate"):
        out["fps"] = round(float(meta["VideoFrameRate"]), 2)
    if meta.get("FileType"):
        out["format"] = str(meta["FileType"])
    return out


# ------------------------------------------------------------------- photos --

# (name, longest edge in pixels, WebP quality)
#   t: two columns on a phone are ~200 CSS px each; 400 px keeps a thumbnail
#      near 15 KB, which is about what one TCP round trip can carry.
#   m: sharp on a phone screen at a few hundred KB.
#   l: close to full detail for pinch-zoom, around a megabyte.
PHOTO_SIZES = (("t", 400, 62), ("m", 1680, 78), ("l", 3200, 84))


def _load_upright(path: str) -> Image.Image:
    try:
        img = Image.open(path)
        img.load()
    except Exception as exc:
        raise MediaError(f"cannot read image: {exc}") from exc
    img = ImageOps.exif_transpose(img)
    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGBA" if "A" in img.getbands() or "transparency" in img.info else "RGB")
    return img


def _placeholder(img: Image.Image) -> str:
    """A ~200-byte blur shipped inside the feed itself, so a tile has the right
    shape and colour before a single image request has come back."""
    tiny = img.convert("RGB")
    tiny.thumbnail((16, 16))
    buf = io.BytesIO()
    tiny.save(buf, "WEBP", quality=40, method=4)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _write_sizes(img: Image.Image, outdir: str, sizes) -> dict:
    """Write each size into outdir. Returns {name: bytes}."""
    icc = img.info.get("icc_profile")   # iPhones shoot Display P3; keep it or colours dull
    written = {}
    work = img
    for name, edge, quality in sorted(sizes, key=lambda s: -s[1]):
        if max(work.size) > edge:
            work = work.copy()
            work.thumbnail((edge, edge), Image.LANCZOS)
        target = os.path.join(outdir, f"{name}.webp")
        kwargs = {"quality": quality, "method": 4}
        if icc:
            kwargs["icc_profile"] = icc
        work.save(target, "WEBP", **kwargs)
        written[name] = os.path.getsize(target)
    return written


def make_photo(path: str, outdir: str) -> dict:
    """Create t/m/l for one photo. Returns what the index needs to know."""
    img = _load_upright(path)
    os.makedirs(outdir, exist_ok=True)
    sizes = _write_sizes(img, outdir, PHOTO_SIZES)
    return {"width": img.width, "height": img.height,
            "placeholder": _placeholder(img), "sizes": sizes}


# ------------------------------------------------------------------- videos --

@dataclass(frozen=True)
class Rung:
    name: str       # also the short edge in pixels
    short: int
    crf: int        # quality target ...
    maxrate: int    # ... but never above this many kbit/s, so a rung fits a link
    audio: int      # kbit/s
    level: str


# Capped-quality H.264. The caps are what make a rung safe to pick for a given
# link speed; the quality target lets quiet footage come in well under them.
#   360   plays on the worst day (~0.08 MB/s)
#   540   the usual starting point
#   720   comfortable on a normal day (~0.3 MB/s)
#   1080  needs ~0.6 MB/s
#   1440  the top: a current phone's screen is about 1179 x 2556, so this is
#         the first size that fills it. Needs ~1.2 MB/s, so it plays
#         live only on a good day; the phone drops to 1080 by itself otherwise.
#         Full-size files are not kept, so the top rung is the best there will
#         ever be without importing the file again.
LADDER = (
    Rung("360", 360, 26, 550, 64, "3.0"),
    Rung("540", 540, 25, 1200, 96, "3.1"),
    Rung("720", 720, 24, 2400, 96, "3.1"),
    Rung("1080", 1080, 23, 4800, 128, "4.0"),
    Rung("1440", 1440, 22, 9000, 160, "5.0"),
)
_LEVEL_HEX = {"3.0": "1e", "3.1": "1f", "4.0": "28", "5.0": "32"}
HDR_TRANSFERS = {"smpte2084", "arib-std-b67"}   # PQ and HLG (iPhone Dolby Vision is HLG)

# HDR -> SDR. Without this, iPhone HDR footage comes out grey and washed.
_TONEMAP = ("zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,"
            "tonemap=tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv,format=yuv420p")


def _ffmpeg() -> str:
    if not config.FFMPEG:
        raise MediaError("ffmpeg is not installed")
    return config.FFMPEG


def video_frame_info(path: str) -> dict:
    """Decode one frame and report how it will be DISPLAYED.

    ffmpeg applies the phone's rotation flag before the frame reaches a filter,
    so the size reported here is upright: a portrait iPhone clip stored as
    3840x2160 comes back as 2160x3840. It also reports the transfer curve,
    which is the only reliable way to tell HDR footage from ordinary.
    """
    cmd = [_ffmpeg(), "-nostdin", "-hide_banner"] + _FROM_OUTSIDE + [
        "-i", path, "-map", "0:v:0", "-frames:v", "1", "-vf", "showinfo", "-f", "null", "-"]
    run = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    log = run.stderr
    size = re.search(r"showinfo.*? s:(\d+)x(\d+)", log)
    if not size:
        tail = " | ".join(log.strip().splitlines()[-3:])
        raise MediaError(f"cannot decode video: {tail[:300]}")
    trc = re.search(r"color_trc:(\S+)", log)
    pix = re.search(r"showinfo.*? fmt:(\S+)", log)
    # A phone held upright stores the picture sideways plus this flag.
    turn = re.search(r"displaymatrix: rotation of (-?[\d.]+) degrees", log)
    return {"width": int(size.group(1)), "height": int(size.group(2)),
            "trc": trc.group(1) if trc else "unknown",
            "pix": pix.group(1) if pix else "yuv420p",
            "rotation": round(-float(turn.group(1))) % 360 if turn else 0,
            "has_audio": bool(re.search(r"Stream #0:\d+.*?: Audio:", log))}


def plan_ladder(width: int, height: int) -> list[tuple[Rung, int, int]]:
    """Which rungs to make for a video of this displayed size, and their sizes.

    Never upscale: a 720p original gets 360/540/720 and no fake 1080 or 1440.
    Dimensions are forced even, which H.264 requires.
    """
    short = min(width, height)
    rungs = [r for r in LADDER if r.short <= short] or [LADDER[0]]
    planned = []
    for r in rungs:
        target = min(r.short, short)
        scale = target / short
        w = max(2, int(round(width * scale / 2)) * 2)
        h = max(2, int(round(height * scale / 2)) * 2)
        planned.append((r, w, h))
    return planned


# --------------------------------------------------------------- hardware --
#
# Reading a 4K iPhone clip back is more than half the work of converting it
# (5.0 of 8.6 CPU-seconds per second of video, measured on real 4K 60 fps HDR
# phone footage),
# and it is exactly the part a video chip does for nothing. So the first two
# steps -- decode, and shrink to the largest size wanted -- are handed to the
# chip when there is one, and everything after that (colour, the four sizes,
# compression) stays on the software path, which is the one that is tested.
#
# Nothing here is assumed to work. At start-up each candidate is put through a
# real conversion of a small test clip, and the first that produces correct
# output is used. A file the chip then trips over is simply redone in
# software. The admin page says which path is in use and why.

SOFTWARE = {"stage": "software", "api": None, "driver": None}

_APIS = {
    # Intel on Linux: what most x86 NAS boxes have (Quick Sync).
    "vaapi": {"frames": "vaapi", "scale": "scale_vaapi=w={w}:h={h}:format={fmt}",
              "ten": ("p010", "p010le"), "eight": ("nv12", "nv12")},
    # Apple: only ever used to exercise this code on the development Mac.
    "videotoolbox": {"frames": "videotoolbox_vld", "scale": "scale_vt=w={w}:h={h}",
                     "ten": ("p010", "p010le"), "eight": ("nv12", "nv12")},
}

hardware = {"use": SOFTWARE, "why": "not checked yet", "tried": [], "fallbacks": 0, "last_error": None}
_hw_lock = threading.Lock()


def _hw_label(choice: dict) -> str:
    if choice["stage"] == "software":
        return "software"
    what = "decode and resize" if choice["stage"] == "gpu-scale" else "decode"
    return f"{what} on the video chip ({choice['api']}{', ' + choice['driver'] if choice['driver'] else ''})"


def _hw_input(choice: dict) -> list[str]:
    if choice["stage"] == "software":
        return []
    args = ["-hwaccel", choice["api"]]
    if choice["api"] == "vaapi":
        args += ["-hwaccel_device", config.VAAPI_DEVICE]
    if choice["stage"] == "gpu-scale":
        # Frames stay on the chip, so ffmpeg cannot turn them upright itself;
        # that is done by hand after they come back (see _head). The file's own
        # rotation flag is cancelled here, or it would be copied onto the
        # result and the picture turned a second time when played.
        args += ["-hwaccel_output_format", _APIS[choice["api"]]["frames"], "-noautorotate",
                 "-display_rotation:v:0", "0"]
    return args


def _hw_env(choice: dict) -> dict | None:
    if not choice.get("driver"):
        return None
    return {**os.environ, "LIBVA_DRIVER_NAME": choice["driver"]}


def _head(choice: dict, info: dict, fps: float | None, top_w: int, top_h: int) -> list[str]:
    """The filters that take a decoded frame to "upright, at the largest size
    wanted, in ordinary memory". After this point every path is identical."""
    chain = []
    if fps and fps > 30.5:
        chain.append("fps=30")          # 60 fps doubles the data for little gain on a phone
    if choice["stage"] != "gpu-scale":
        if (info["width"], info["height"]) != (top_w, top_h):
            # Shrink BEFORE tone-mapping: the HDR maths then runs on a quarter of
            # the pixels of a 4K frame, which is most of the cost on a small CPU.
            chain.append(f"scale={top_w}:{top_h}:flags=bicubic")
        return chain
    api = _APIS[choice["api"]]
    on_chip, in_memory = api["ten"] if "10" in info["pix"] else api["eight"]
    turned = info["rotation"] in (90, 270)
    # The chip sees the frame as stored (sideways for a portrait clip), so it
    # is asked for the target size with width and height swapped.
    w, h = (top_h, top_w) if turned else (top_w, top_h)
    chain.append(api["scale"].format(w=w, h=h, fmt=on_chip))
    chain += ["hwdownload", f"format={in_memory}"]
    chain += {90: ["transpose=1"], 270: ["transpose=2"], 180: ["hflip", "vflip"]}.get(info["rotation"], [])
    return chain


def _glimpse(video_file: str) -> bytes | None:
    """The first frame as a tiny grey picture: enough to tell whether two
    conversions of the same clip show the same thing the same way up."""
    run = subprocess.run([_ffmpeg(), "-nostdin", "-hide_banner", "-loglevel", "error", "-i", video_file,
                          "-frames:v", "1", "-vf", "scale=48:48,format=gray", "-f", "rawvideo", "-"],
                         capture_output=True, timeout=60)
    return run.stdout if run.returncode == 0 and len(run.stdout) == 48 * 48 else None


def _same_picture(a: bytes | None, b: bytes | None) -> bool:
    if not a or not b:
        return False
    return sum(abs(x - y) for x, y in zip(a, b)) / len(a) < 10      # out of 255


def _candidates() -> tuple[list[dict], str]:
    if config.HWACCEL == "off":
        return [], "switched off (WATERFALL_HWACCEL=off)"
    if sys.platform == "darwin":
        apis = [("videotoolbox", None)]
    elif os.path.exists(config.VAAPI_DEVICE):
        # Two drivers can serve an Intel chip of the Gemini Lake generation
        # and its neighbours; which one works depends on the machine, so each
        # is tried.
        apis = [("vaapi", None), ("vaapi", "iHD"), ("vaapi", "i965")]
    else:
        return [], f"no video chip was passed to the container ({config.VAAPI_DEVICE} is not there)"
    return ([{"stage": stage, "api": api, "driver": driver}
             for stage in ("gpu-scale", "decode") for api, driver in apis], "")


def _device_note() -> str:
    """Who may use the video chip, in words that say what to change."""
    try:
        st = os.stat(config.VAAPI_DEVICE)
    except OSError:
        return ""
    if os.access(config.VAAPI_DEVICE, os.R_OK | os.W_OK):
        return ""
    return (f" The chip belongs to group {st.st_gid} and this container is in groups "
            f"{sorted(set(os.getgroups()))}: put \"{st.st_gid}\" under group_add in the compose text.")


def probe_hardware() -> dict:
    """Find the fastest way of reading video that really works here, by trying.

    The test clip is the hard case in miniature: 10-bit HEVC, stored sideways
    with a rotation flag, as an iPhone records. A candidate passes only if the
    converted result has the right shape.
    """
    with _hw_lock:
        candidates, why = _candidates()
        tried = []
        hardware.update(use=SOFTWARE, why=why or "no candidate worked", tried=tried)
        if not candidates:
            return hardware
        scratch = tempfile.mkdtemp(prefix="hwtest-", dir=config.DATA_DIR if os.path.isdir(config.DATA_DIR) else None)
        try:
            clip = os.path.join(scratch, "test.mov")
            raw = os.path.join(scratch, "raw.mov")
            quiet = [_ffmpeg(), "-nostdin", "-hide_banner", "-loglevel", "error", "-y"]
            made = subprocess.run(
                quiet + ["-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30", "-t", "1",
                         "-c:v", "libx265", "-preset", "ultrafast", "-pix_fmt", "yuv420p10le",
                         "-x265-params", "log-level=error", "-tag:v", "hvc1", raw],
                capture_output=True, text=True, timeout=120)
            if made.returncode == 0:
                made = subprocess.run(quiet + ["-display_rotation:v:0", "-90", "-i", raw, "-c", "copy", clip],
                                      capture_output=True, text=True, timeout=60)
            if made.returncode != 0:
                hardware["why"] = "could not make the test clip: " + made.stderr.strip()[-200:]
                return hardware
            info = video_frame_info(clip)
            planned = plan_ladder(info["width"], info["height"])
            top = planned[-1]
            # The answer to compare against: the same clip done in software.
            truth_dir = os.path.join(scratch, "software")
            os.makedirs(truth_dir)
            run = subprocess.run(_ladder_command(clip, truth_dir, info, 30, planned, SOFTWARE),
                                 capture_output=True, text=True, timeout=120)
            truth = _glimpse(os.path.join(truth_dir, f"{top[0].name}.m4s")) if run.returncode == 0 else None
            if truth is None:
                hardware["why"] = "the software conversion of the test clip failed, so there was nothing to compare with"
                return hardware
            for choice in candidates:
                work = os.path.join(scratch, f"{choice['stage']}-{choice['api']}-{choice['driver'] or 'default'}")
                os.makedirs(work)
                cmd = _ladder_command(clip, work, info, 30, planned, choice)
                try:
                    run = subprocess.run(cmd, capture_output=True, text=True, timeout=120, env=_hw_env(choice))
                    ok = run.returncode == 0
                    note = " | ".join(run.stderr.strip().splitlines()[-2:])[-240:]
                    if ok:
                        made = os.path.join(work, f"{top[0].name}.m4s")
                        out = video_frame_info(made)
                        ok = (out["width"], out["height"]) == (top[1], top[2])
                        note = "" if ok else f"wrong shape {out['width']}x{out['height']}"
                        if ok and not _same_picture(truth, _glimpse(made)):
                            # Right shape is not enough: upside down has the right shape.
                            ok, note = False, "the picture does not match the software result"
                except (subprocess.TimeoutExpired, MediaError) as exc:
                    ok, note = False, str(exc)[-240:]
                tried.append({"what": _hw_label(choice), "ok": ok, "note": note})
                if ok:
                    hardware.update(use=choice, why="")
                    return hardware
            hardware["why"] = "the video chip is there but nothing worked with it." + _device_note()
            return hardware
        finally:
            shutil.rmtree(scratch, ignore_errors=True)


def hardware_report() -> dict:
    return {"in_use": hardware["use"]["stage"] != "software", "path": _hw_label(hardware["use"]),
            "why": hardware["why"], "tried": hardware["tried"], "fallbacks": hardware["fallbacks"],
            "last_error": hardware["last_error"]}


def _ladder_command(path: str, workdir: str, info: dict, fps: float | None,
                    planned: list[tuple[Rung, int, int]], choice: dict = SOFTWARE) -> list[str]:
    top_w, top_h = planned[-1][1], planned[-1][2]
    chain = _head(choice, info, fps, top_w, top_h)
    chain.append(_TONEMAP if info["trc"] in HDR_TRANSFERS else "format=yuv420p")
    n = len(planned)
    graph = f"[0:v:0]{','.join(chain)},split={n}" + "".join(f"[s{i}]" for i in range(n))
    for i, (_, w, h) in enumerate(planned):
        graph += f";[s{i}]" + ("null" if (w, h) == (top_w, top_h)
                               else f"scale={w}:{h}:flags=bicubic") + f"[v{i}]"

    cmd = [_ffmpeg(), "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
           "-progress", "pipe:1", "-nostats"]
    if config.FFMPEG_THREADS:
        cmd += ["-threads", str(config.FFMPEG_THREADS)]
    cmd += _hw_input(choice) + _FROM_OUTSIDE + ["-i", path, "-filter_complex", graph]
    for i, (r, _, _) in enumerate(planned):
        cmd += ["-map", f"[v{i}]"]
        if info["has_audio"]:
            cmd += ["-map", "0:a:0", "-c:a", "aac", "-b:a", f"{r.audio}k", "-ac", "2", "-ar", "48000"]
        cmd += ["-c:v", "libx264", "-preset", config.X264_PRESET, "-profile:v", "high",
                "-level", r.level, "-crf", str(r.crf),
                "-maxrate", f"{r.maxrate}k", "-bufsize", f"{r.maxrate * 2}k",
                "-pix_fmt", "yuv420p", "-colorspace", "bt709", "-color_primaries", "bt709",
                "-color_trc", "bt709",
                # A keyframe exactly every 2 s, on every rung, so the player can
                # switch rung at any segment boundary without a glitch.
                "-force_key_frames", "expr:gte(t,n_forced*2)", "-sc_threshold", "0",
                "-map_metadata", "-1",
                # One file per rung, addressed by byte range, instead of
                # hundreds of little segment files per video.
                "-f", "hls", "-hls_time", "4", "-hls_playlist_type", "vod",
                "-hls_segment_type", "fmp4", "-hls_flags", "single_file",
                os.path.join(workdir, f"{r.name}.m3u8")]
    return cmd


def _poster(video_file: str, outdir: str, at: float) -> dict:
    cmd = [_ffmpeg(), "-nostdin", "-hide_banner", "-loglevel", "error", "-ss", f"{at:.2f}",
           "-i", video_file, "-frames:v", "1", "-f", "image2pipe", "-c:v", "png", "-"]
    run = subprocess.run(cmd, capture_output=True, timeout=300)
    if run.returncode != 0 or not run.stdout:
        raise MediaError("could not grab a poster frame")
    img = Image.open(io.BytesIO(run.stdout)).convert("RGB")
    sizes = _write_sizes(img, outdir, PHOTO_SIZES[:2])
    return {"placeholder": _placeholder(img), "sizes": sizes}


# The wall preview: silent, small, a handful of seconds. A short clip is shown
# from the start; a long one is sampled at four points, so a fifteen-minute
# video shows what happens in it rather than its first six seconds of someone
# raising the phone. Around 150-250 KB: a fraction of a second of the link.
PREVIEW_WHOLE_UNDER = 8.0      # seconds: at or under this, just use the opening
PREVIEW_WHOLE_LENGTH = 6.0
PREVIEW_CUTS = (0.10, 0.35, 0.60, 0.85)
PREVIEW_CUT_LENGTH = 1.5


def preview_segments(duration: float) -> list[tuple[float, float]]:
    """(start, length) pieces of the video that make up its wall preview."""
    if duration <= PREVIEW_WHOLE_UNDER:
        return [(0.0, round(min(duration, PREVIEW_WHOLE_LENGTH), 3))]
    # Never let a piece run past the end: on a clip only just over the limit,
    # the last cut would otherwise ask for footage that is not there.
    latest = duration - PREVIEW_CUT_LENGTH
    return [(round(min(duration * at, latest), 3), PREVIEW_CUT_LENGTH) for at in PREVIEW_CUTS]


def _preview(source: str, target: str, duration: float) -> int:
    """Cut the wall preview from the smallest rung, which is already upright,
    ordinary-colour and tiny, so this costs almost nothing next to the ladder."""
    pieces = preview_segments(duration)
    cmd = [_ffmpeg(), "-nostdin", "-hide_banner", "-loglevel", "error", "-y"]
    for start, length in pieces:
        cmd += ["-ss", f"{start:.3f}", "-t", f"{length:.3f}", "-i", source]
    joined = "".join(f"[{i}:v:0]" for i in range(len(pieces)))
    cmd += ["-filter_complex", f"{joined}concat=n={len(pieces)}:v=1:a=0,fps=24[o]",
            "-map", "[o]", "-an", "-c:v", "libx264", "-preset", config.X264_PRESET,
            "-profile:v", "main", "-crf", "30", "-maxrate", "400k", "-bufsize", "800k",
            "-pix_fmt", "yuv420p", "-g", "24", "-movflags", "+faststart", target]
    run = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    if run.returncode != 0 or not os.path.exists(target):
        tail = " | ".join(run.stderr.strip().splitlines()[-2:])
        raise MediaError(f"could not make the wall preview: {tail[:300]}")
    return os.path.getsize(target)


def _playlist_stats(playlist: str) -> tuple[float, int]:
    """-> (duration in seconds, the busiest segment's bitrate in bits/s).

    The player decides whether a rung fits the link from its PEAK, so the peak
    advertised has to be the real one. Quoting the encoder's ceiling instead
    would make quiet footage look three times heavier than it is, and the
    phone would refuse to step up to a rung it could easily play.
    """
    total, peak, seconds = 0.0, 0.0, 0.0
    with open(playlist, encoding="utf-8") as f:
        for line in f:
            if line.startswith("#EXTINF:"):
                seconds = float(line[8:].split(",")[0])
                total += seconds
            elif line.startswith("#EXT-X-BYTERANGE:") and seconds > 0:
                size = int(line[17:].split("@")[0])
                peak = max(peak, size * 8 / seconds)
    return total, int(peak)


def make_video(path: str, outdir: str, fps: float | None = None, progress=None) -> dict:
    """Create the poster and the whole ladder for one video.

    The ladder is built in a scratch folder and moved into place only when
    every rung is complete, so the server can never hand out half a video.
    """
    info = video_frame_info(path)
    planned = plan_ladder(info["width"], info["height"])
    os.makedirs(outdir, exist_ok=True)
    for stale in os.listdir(outdir):        # a ladder abandoned by an earlier run
        if stale.startswith(".v-"):
            shutil.rmtree(os.path.join(outdir, stale), ignore_errors=True)
    work = os.path.join(outdir, f".v-{os.getpid()}")

    def lower_priority():
        try:
            os.nice(config.NICE)
        except OSError:
            pass

    def attempt(choice: dict) -> tuple[int, str]:
        shutil.rmtree(work, ignore_errors=True)
        os.makedirs(work)
        cmd = _ladder_command(path, work, info, fps, planned, choice)
        # ffmpeg's error text goes to a file, not a pipe: a pipe nobody is reading
        # fills up and freezes the conversion on a long, chatty job.
        with open(os.path.join(work, "ffmpeg.log"), "w+", encoding="utf-8", errors="replace") as log, \
                subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=log, text=True,
                                 preexec_fn=lower_priority, env=_hw_env(choice)) as proc:
            _running.add(proc)
            try:
                for line in proc.stdout:
                    if progress and line.startswith("out_time_us="):
                        value = line.split("=", 1)[1].strip()
                        if value.isdigit():
                            progress(int(value) / 1_000_000)
                code = proc.wait()
            except BaseException:
                proc.kill()
                proc.wait()
                shutil.rmtree(work, ignore_errors=True)
                raise
            finally:
                _running.discard(proc)
            log.seek(0)
            err = log.read()
        os.remove(os.path.join(work, "ffmpeg.log"))
        if code == 0:
            # Right shape, or it did not work, whatever the exit code says.
            try:
                top = planned[-1]
                made = video_frame_info(os.path.join(work, f"{top[0].name}.m4s"))
                if (made["width"], made["height"]) != (top[1], top[2]):
                    code, err = 1, f"wrong shape {made['width']}x{made['height']}"
            except MediaError as exc:
                code, err = 1, str(exc)
        return code, err

    used = hardware["use"]
    code, err = attempt(used)
    if code != 0 and used["stage"] != "software":
        # The chip tripped on this file. Redo it the tested way and carry on.
        hardware["fallbacks"] += 1
        hardware["last_error"] = " | ".join(err.strip().splitlines()[-2:])[-300:]
        used = SOFTWARE
        code, err = attempt(used)
    if code != 0:
        shutil.rmtree(work, ignore_errors=True)
        tail = " | ".join(err.strip().splitlines()[-3:])
        raise MediaError(f"ffmpeg failed: {tail[:400]}")

    rungs = []
    duration = 0.0
    for r, w, h in planned:
        playlist = os.path.join(work, f"{r.name}.m3u8")
        media = os.path.join(work, f"{r.name}.m4s")
        if not os.path.exists(playlist) or not os.path.exists(media):
            shutil.rmtree(work, ignore_errors=True)
            raise MediaError(f"rung {r.name} was not produced")
        seconds, peak = _playlist_stats(playlist)
        duration = max(duration, seconds)
        size = os.path.getsize(media)
        rungs.append({"name": r.name, "w": w, "h": h, "bytes": size, "peak": peak,
                      "codecs": f"avc1.6400{_LEVEL_HEX[r.level]}"
                                + (",mp4a.40.2" if info["has_audio"] else "")})
    if duration <= 0:
        shutil.rmtree(work, ignore_errors=True)
        raise MediaError("the converted video is empty")
    for rung in rungs:
        rung["avg"] = int(rung["bytes"] * 8 / duration)

    poster = _poster(os.path.join(work, f"{planned[-1][0].name}.m4s"), outdir,
                     min(1.0, duration / 2))
    preview_bytes = _preview(os.path.join(work, f"{planned[0][0].name}.m4s"),
                             os.path.join(outdir, "p.mp4"), duration)
    final = os.path.join(outdir, "v")
    shutil.rmtree(final, ignore_errors=True)
    os.replace(work, final)
    return {"width": info["width"], "height": info["height"], "duration": round(duration, 3),
            "hdr": info["trc"] in HDR_TRANSFERS, "has_audio": info["has_audio"],
            "made_by": _hw_label(used),
            "rungs": rungs, "placeholder": poster["placeholder"],
            "sizes": {**poster["sizes"], "p": preview_bytes}}
