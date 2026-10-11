"""Turn a GPS position into "City, Country" with no internet connection.

A phone records where a photo was taken as two numbers. Showing a place name
normally means asking a map service, and Waterfall must work on a network that
may have no route to one (and should not be telling anyone where your photos
were taken in any case). So the names travel with the application: a list of
the world's towns from GeoNames (CC BY 4.0, see data/ATTRIBUTION.txt), trimmed
to name, country, position and size. How small a town it reaches depends on
which GeoNames list a copy was built from, and it can be rebuilt with every
named village of chosen countries added, for places where population figures
are mostly missing and the worldwide list holds only the main towns
(scripts/make-places.py).

The answer is the town a person would name, which is not always the nearest
dot on the map: the nearest listed place to the Eiffel Tower is "Paris 16
Passy", and to Tokyo Station it is the ward of Chuo. So a big city pulls in the
points around its centre (size divided by distance cubed), while a town twenty
kilometres out still gets its own name. It is not a boundary lookup, and near
the edge of a city it can pick the neighbour.

That rule is right for Paris and wrong for the places you know best. A beach
village should be named for itself, not for the town down the road, and an
island for itself, not for the nearest village on the bank. No formula knows
that, so there is a short hand-made list: a name, a position and a radius. A
photo taken inside a listed circle gets that name, whatever the general rule
would say. One such list ships in `data/places-preferred.tsv`; your own, in the
same format, is read from the data folder as well (config.PLACES_FILE).
"""

from __future__ import annotations

import gzip
import math
import os
import threading

from . import config

_DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
MAX_KM = 120          # further than this from any town: say nothing rather than mislead
# A village with no population on record. Low enough that a real town within
# about ten kilometres is named instead; high enough that, out in the country,
# the village is named rather than a town an hour's drive away.
UNKNOWN_SIZE = 300

_preferred: list[tuple[float, float, str, str, float]] = []
_lock = threading.Lock()
_grid: dict[tuple[int, int], list[tuple[float, float, str, str, int]]] | None = None
_countries: dict[str, str] = {}


def _load() -> None:
    """Read the list once, into one-degree squares so a lookup scans a handful
    of towns instead of all of them."""
    global _grid
    with _lock:
        if _grid is not None:
            return
        grid: dict[tuple[int, int], list] = {}
        try:
            with open(os.path.join(_DATA, "countries.tsv"), encoding="utf-8") as f:
                for line in f:
                    code, name = line.rstrip("\n").split("\t")
                    _countries[code] = name
            with gzip.open(os.path.join(_DATA, "places.tsv.gz"), "rt", encoding="utf-8") as f:
                for line in f:
                    code, name, lat, lon, people = line.rstrip("\n").split("\t")
                    lat, lon = float(lat), float(lon)
                    grid.setdefault((math.floor(lat), math.floor(lon)), []).append(
                        (lat, lon, name, code, int(people) if int(people) > 0 else UNKNOWN_SIZE))
        except OSError:
            grid = {}        # no list shipped: places are simply not named
        _preferred.clear()
        # The list that ships, then the owner's own. One damaged line loses
        # that line, never the rest: the second file is written by hand.
        for path in (os.path.join(_DATA, "places-preferred.tsv"), config.PLACES_FILE):
            try:
                with open(path, encoding="utf-8") as f:
                    lines = f.read(1 << 20).splitlines()
            except (OSError, ValueError):
                continue
            for line in lines:
                if not line.strip() or line.startswith("#"):
                    continue
                try:
                    name, code, lat, lon, radius = line.split("\t")[:5]
                    _preferred.append((float(lat), float(lon), name.strip(), code.strip(), float(radius)))
                except ValueError:
                    pass
        _grid = grid


def _km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 12742 * math.asin(math.sqrt(a))


def nearest(lat: float, lon: float) -> str | None:
    """"City, Country" for a position, or None if nowhere listed is near."""
    if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    _load()
    # Hand-made names first. Where circles overlap, the one whose centre is
    # nearest in proportion to its own size wins, so a small precise place
    # inside a large general one is still found.
    chosen, closeness = None, 1.0
    for plat, plon, name, code, radius in _preferred:
        d = _km(lat, lon, plat, plon)
        if radius > 0 and d / radius <= closeness:
            chosen, closeness = (name, code), d / radius
    if chosen:
        return f"{chosen[0]}, {_countries.get(chosen[1], chosen[1])}"
    best, best_pull = None, 0.0
    row, col = math.floor(lat), math.floor(lon)
    # A degree of longitude shrinks towards the poles, so look wider there.
    reach_lon = min(180, 1 + int(MAX_KM / (111.3 * max(0.05, math.cos(math.radians(lat))))))
    for r in range(row - 2, row + 3):
        for c in range(col - reach_lon, col + reach_lon + 1):
            for plat, plon, name, code, people in _grid.get((r, (c + 180) % 360 - 180), ()):
                d = _km(lat, lon, plat, plon)
                if d > MAX_KM:
                    continue
                pull = people / (d + 2) ** 3
                if pull > best_pull:
                    best, best_pull = (name, code), pull
    if not best:
        return None
    name, code = best
    country = _countries.get(code, code)
    return f"{name}, {country}"
