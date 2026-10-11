"""Stand-in media, generated on demand.

No photo or video is ever committed to this repository (the real ones are
private and the .gitignore refuses them), so the tests and the development
library both make their own. The files imitate what an iPhone produces closely
enough to exercise the same code paths: HEIC with EXIF and GPS, portrait 4K
HEVC with an HDR transfer curve and a rotation flag, and a Live Photo pair.
"""

from __future__ import annotations

import colorsys
import os
import subprocess

from PIL import Image, ImageDraw, ImageFont

from waterfall import config

try:
    import pillow_heif
    pillow_heif.register_heif_opener()
except Exception:  # pragma: no cover
    pillow_heif = None


def _dms(value: float):
    value = abs(value)
    degrees = int(value)
    minutes = int((value - degrees) * 60)
    seconds = round((value - degrees - minutes / 60) * 3600, 2)
    return (float(degrees), float(minutes), float(seconds))


def photo(path: str, width: int, height: int, label: str, hue: float = 0.6,
          when: str = "2025:07:04 17:30:00", offset: str = "+02:00",
          gps: tuple[float, float] | None = None, orientation: int = 1) -> str:
    """A labelled gradient with believable iPhone EXIF. Format follows the
    file extension (.heic or .jpg)."""
    top = tuple(int(c * 255) for c in colorsys.hsv_to_rgb(hue, 0.55, 0.95))
    bottom = tuple(int(c * 255) for c in colorsys.hsv_to_rgb((hue + 0.12) % 1, 0.8, 0.35))
    img = Image.new("RGB", (width, height))
    draw = ImageDraw.Draw(img)
    for y in range(height):
        t = y / max(1, height - 1)
        draw.line([(0, y), (width, y)],
                  fill=tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)))
    try:
        font = ImageFont.load_default(size=max(24, min(width, height) // 9))
    except TypeError:  # very old Pillow
        font = ImageFont.load_default()
    draw.multiline_text((width / 2, height / 2), label, fill="white", font=font,
                        anchor="mm", align="center", stroke_width=3, stroke_fill="black")

    exif = Image.Exif()
    exif[0x010F] = "Apple"
    exif[0x0110] = "iPhone 15 Pro"
    exif[0x0112] = orientation
    sub = exif.get_ifd(0x8769)
    sub[0x9003] = when
    sub[0x9011] = offset
    sub[0x829D] = 1.78           # FNumber
    sub[0x829A] = 1 / 120        # ExposureTime
    sub[0x8827] = 80             # ISO
    sub[0xA405] = 24             # FocalLengthIn35mmFormat
    sub[0xA434] = "iPhone 15 Pro back triple camera 6.765mm f/1.78"
    if gps:
        g = exif.get_ifd(0x8825)
        g[1] = "N" if gps[0] >= 0 else "S"
        g[2] = _dms(gps[0])
        g[3] = "E" if gps[1] >= 0 else "W"
        g[4] = _dms(gps[1])

    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    if path.lower().endswith((".heic", ".heif")):
        img.save(path, format="HEIF", quality=80, exif=exif.tobytes())
    else:
        img.save(path, quality=88, exif=exif)
    return path


def video(path: str, width: int, height: int, seconds: float, fps: int = 30,
          hdr: bool = False, rotate: int = 0, source: str = "testsrc2",
          when: str = "2025-07-04T17:30:00+0200", audio: bool = True) -> str:
    """A synthetic clip. hdr=True makes 10-bit HEVC tagged HLG, which is what an
    iPhone records by default; rotate sets the display-rotation flag, which is
    how a portrait iPhone clip is stored (sideways, plus a flag)."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    ff = config.FFMPEG
    cmd = [ff, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
           "-f", "lavfi", "-i", f"{source}=size={width}x{height}:rate={fps}"]
    if audio:
        cmd += ["-f", "lavfi", "-i", "sine=frequency=330:sample_rate=48000"]
    cmd += ["-t", str(seconds)]
    if hdr:
        cmd += ["-c:v", "libx265", "-preset", "ultrafast", "-pix_fmt", "yuv420p10le",
                "-x265-params", "log-level=error:colorprim=bt2020:transfer=arib-std-b67:"
                                "colormatrix=bt2020nc",
                "-color_primaries", "bt2020", "-color_trc", "arib-std-b67",
                "-colorspace", "bt2020nc", "-tag:v", "hvc1"]
    else:
        cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]
    if audio:
        cmd += ["-c:a", "aac", "-b:a", "96k"]
    raw = path + ".raw.mov"
    subprocess.run(cmd + [raw], check=True)
    # Second pass: the rotation flag and Apple's own metadata keys.
    tag = [ff, "-nostdin", "-hide_banner", "-loglevel", "error", "-y"]
    if rotate:
        tag += ["-display_rotation:v:0", str(-rotate)]
    tag += ["-i", raw, "-c", "copy",
            "-metadata", f"com.apple.quicktime.creationdate={when}",
            "-metadata", "com.apple.quicktime.make=Apple",
            "-metadata", "com.apple.quicktime.model=iPhone 15 Pro",
            "-movflags", "use_metadata_tags", path]
    subprocess.run(tag, check=True)
    os.remove(raw)
    return path


def library(root: str, big: bool = True) -> None:
    """A small library shaped like the real thing: nested sections, mixed
    photos and videos, a Live Photo pair, and loose files in the root."""
    n = 0

    def shots(folder: str, names: list[str], hue: float, year: int, heic: bool = True):
        nonlocal n
        for i, name in enumerate(names):
            n += 1
            wide = i % 3 == 0
            w, h = (2016, 1512) if wide else (1512, 2016)
            if i % 5 == 4:
                w, h = 2400, 1100            # a panorama-ish frame
            ext = "HEIC" if heic and i % 4 != 3 else "jpg"
            photo(os.path.join(root, folder, f"IMG_{1000 + n}.{ext}"), w, h, name,
                  hue=(hue + i * 0.03) % 1, when=f"{year}:{1 + i % 12:02d}:{3 + i:02d} 1{i % 10}:15:00",
                  gps=(59.9 + i * 0.01, 10.7 + i * 0.01) if i % 2 == 0 else None)

    shots("Travel/Norway", [f"Norway\n{i}" for i in range(1, 10)], 0.55, 2024)
    shots("Travel/Japan", [f"Japan\n{i}" for i in range(1, 8)], 0.95, 2023)
    shots("Family", [f"Family\n{i}" for i in range(1, 9)], 0.08, 2025)
    shots("Pets", [f"Pets\n{i}" for i in range(1, 7)], 0.30, 2022)
    shots("Private", [f"Private\n{i}" for i in range(1, 4)], 0.75, 2021)
    shots("", ["Loose\n1", "Loose\n2"], 0.15, 2020, heic=False)

    video(os.path.join(root, "Pets", "IMG_3001.mp4"), 1920, 1080, 12, source="mandelbrot")
    video(os.path.join(root, "Travel", "Norway", "IMG_3002.MOV"), 1280, 720, 8, source="smptebars")
    if big:
        # The case that matters: portrait, 4K, HDR, as an iPhone shoots it.
        video(os.path.join(root, "Family", "IMG_3003.MOV"), 3840, 2160, 10, hdr=True, rotate=90)
        video(os.path.join(root, "Travel", "Japan", "IMG_3004.MOV"), 3840, 2160, 8, fps=60, hdr=True)
    # A Live Photo: a HEIC and a short MOV sharing a name. Only the HEIC should
    # appear in the feed.
    photo(os.path.join(root, "Family", "IMG_4001.HEIC"), 1512, 2016, "Live\nPhoto", hue=0.02)
    video(os.path.join(root, "Family", "IMG_4001.MOV"), 1440, 1080, 2.5, rotate=90)
