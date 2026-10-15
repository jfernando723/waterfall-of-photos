"""Draw Waterfall's icon, once, into every size a browser or a phone asks for.

    .venv/bin/python scripts/make-icons.py

Three columns of photo tiles fall down a dark square, pale aqua at the top and
deep blue at the bottom: the wall of pictures, and a waterfall. The shapes are
written here once and become:

  waterfall/static/icon.svg              the tab icon, for browsers that take a drawing
  waterfall/static/favicon.ico           the tab icon at 16, 32 and 48 pixels
  waterfall/static/apple-touch-icon.png  180 x 180: an iPhone's or an iPad's Home Screen
  waterfall/static/icon-192.png          the web app manifest's two sizes, for Android
  waterfall/static/icon-512.png          and for Chrome on a computer

The tab icons have rounded corners of their own. The others fill the square to
its edges, because a phone cuts its own corners and fills any see-through part
with black or white. Uses Pillow, which Waterfall needs anyway; nothing else.
"""

import io
import os

from PIL import Image, ImageDraw, ImageFilter

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "waterfall", "static")
SIZE = 1024                          # the drawing's own units
CORNER = 228                         # the tab icon's rounded corners
BACK = ("#151b26", "#08090c")        # the square, top and bottom; the page is #0b0b0d
WATER = (("0", "#c4f1ff"), ("0.45", "#4cb4f5"), ("1", "#1146b8"))   # down the tiles
GAP = 52
LEFT = 156
WIDTH = (SIZE - 2 * LEFT - 2 * GAP) // 3
RADIUS = 46
# The mist where the water lands: a white glow across the foot of the square.
MIST = (512, 1075, 640, 175)         # centre x, centre y, half-width, half-height
MIST_STRENGTH = 0.62
# Each column's tiles, top to bottom, as (top, bottom). They run off the square
# at both ends: the wall, like the water, does not stop.
COLUMNS = [
    [(-120, 236), (288, 560), (612, 1150)],
    [(-120, 92), (144, 470), (522, 742), (794, 1150)],
    [(-120, 352), (404, 650), (702, 1150)],
]


def _hex(colour):
    return tuple(int(colour[i:i + 2], 16) for i in (1, 3, 5))


def _between(stops, t):
    stops = [(float(at), _hex(c)) for at, c in stops]
    for (a, ca), (b, cb) in zip(stops, stops[1:]):
        if t <= b:
            f = 0 if b == a else (t - a) / (b - a)
            return tuple(round(x + (y - x) * f) for x, y in zip(ca, cb))
    return stops[-1][1]


def _gradient(stops, size):
    column = Image.new("RGB", (1, size))
    for y in range(size):
        column.putpixel((0, y), _between(stops, y / (size - 1)))
    return column.resize((size, size))


def draw(size, rounded):
    """The icon at `size` pixels, drawn four times larger and shrunk, so its
    edges are smooth."""
    big = size * 4 if size < 256 else size
    k = big / SIZE
    picture = _gradient((("0", BACK[0]), ("1", BACK[1])), big).convert("RGBA")
    tiles = Image.new("L", (big, big), 0)
    pen = ImageDraw.Draw(tiles)
    for i, column in enumerate(COLUMNS):
        x = LEFT + i * (WIDTH + GAP)
        for top, bottom in column:
            pen.rounded_rectangle([x * k, top * k, (x + WIDTH) * k, bottom * k], radius=RADIUS * k, fill=255)
    picture.paste(_gradient(WATER, big), (0, 0), tiles)
    cx, cy, rx, ry = (v * k for v in MIST)
    mist = Image.new("L", (big, big), 0)
    ImageDraw.Draw(mist).ellipse([cx - rx, cy - ry, cx + rx, cy + ry], fill=round(255 * MIST_STRENGTH))
    mist = mist.filter(ImageFilter.GaussianBlur(ry * 0.45))
    picture.paste(Image.new("RGB", (big, big), "#ffffff"), (0, 0), mist)
    if rounded:
        shape = Image.new("L", (big, big), 0)
        ImageDraw.Draw(shape).rounded_rectangle([0, 0, big - 1, big - 1], radius=CORNER * k, fill=255)
        picture.putalpha(shape)
    return picture.resize((size, size), Image.LANCZOS) if big != size else picture


def svg():
    tiles = []
    for i, column in enumerate(COLUMNS):
        x = LEFT + i * (WIDTH + GAP)
        for top, bottom in column:
            tiles.append(f'<rect x="{x}" y="{top}" width="{WIDTH}" height="{bottom - top}" rx="{RADIUS}"/>')
    stops = "".join(f'<stop offset="{at}" stop-color="{c}"/>' for at, c in WATER)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {SIZE} {SIZE}">'
            f'<title>Waterfall</title>'
            f'<defs><linearGradient id="b" x1="0" y1="0" x2="0" y2="1">'
            f'<stop offset="0" stop-color="{BACK[0]}"/><stop offset="1" stop-color="{BACK[1]}"/></linearGradient>'
            f'<linearGradient id="w" x1="0" y1="0" x2="0" y2="{SIZE}" gradientUnits="userSpaceOnUse">{stops}</linearGradient>'
            f'<radialGradient id="m" cx="{MIST[0]}" cy="{MIST[1]}" r="{MIST[2]}" gradientUnits="userSpaceOnUse"'
            f' gradientTransform="translate(0 {MIST[1]}) scale(1 {MIST[3] / MIST[2]:.3f}) translate(0 {-MIST[1]})">'
            f'<stop offset="0" stop-color="#fff" stop-opacity="{MIST_STRENGTH}"/>'
            f'<stop offset="0.55" stop-color="#fff" stop-opacity="{MIST_STRENGTH * 0.55:.2f}"/>'
            f'<stop offset="1" stop-color="#fff" stop-opacity="0"/></radialGradient>'
            f'<clipPath id="c"><rect width="{SIZE}" height="{SIZE}" rx="{CORNER}"/></clipPath></defs>'
            f'<g clip-path="url(#c)"><rect width="{SIZE}" height="{SIZE}" fill="url(#b)"/>'
            f'<g fill="url(#w)">{"".join(tiles)}</g>'
            f'<rect width="{SIZE}" height="{SIZE}" fill="url(#m)"/></g></svg>\n')


def main():
    with open(os.path.join(STATIC, "icon.svg"), "w") as f:
        f.write(svg())
    # A .ico holds each size drawn for itself, not one picture shrunk by the browser.
    sizes = [48, 32, 16]
    largest, *rest = [draw(n, rounded=True) for n in sizes]
    largest.save(os.path.join(STATIC, "favicon.ico"), format="ICO", sizes=[(n, n) for n in sizes],
                 append_images=rest)
    for name, n in (("apple-touch-icon.png", 180), ("icon-192.png", 192), ("icon-512.png", 512)):
        out = io.BytesIO()
        draw(n, rounded=False).convert("RGB").save(out, format="PNG", optimize=True)
        with open(os.path.join(STATIC, name), "wb") as f:
            f.write(out.getvalue())
    print("written to", os.path.normpath(STATIC))


if __name__ == "__main__":
    main()
