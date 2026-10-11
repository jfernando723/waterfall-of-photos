#!/usr/bin/env python3
"""Rebuild waterfall/data/places.tsv.gz and countries.tsv from GeoNames.

    python3 scripts/make-places.py cities500.txt countryInfo.txt [SL.txt TD.txt ...]

All inputs come from https://download.geonames.org/export/dump/ . GeoNames data
is licensed CC BY 4.0; the attribution is in waterfall/data/ATTRIBUTION.txt,
which travels with the data, and in THIRD-PARTY-NOTICES.md.

  cities500.txt   every town in the world with 500 people or more. This is the
                  base: 21,000 places in the United States, 15,000 in France,
                  and so on, which is what makes "City, Country" right in
                  most of the world.
  XX.txt ...      OPTIONAL whole-country files (named by two-letter country
                  code), for countries you know well where the base list is
                  thin. In much of the world almost no village has a
                  population figure on record, so the base list holds only the
                  main towns there. These files add every named village,
                  marked as size-unknown so that a real town nearby still wins
                  and a village is named only where nothing bigger is. Each
                  adds roughly 10,000 to 15,000 places for a mid-sized country.

Only five columns are kept, so the list can live in the repository and inside
the image, and Waterfall can name a place with no internet connection at all.
"""
import gzip
import os
import sys

cities, countries, extras = sys.argv[1], sys.argv[2], sys.argv[3:]
out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "waterfall", "data")
os.makedirs(out, exist_ok=True)

seen, rows = set(), []


def take(path: str, villages_too: bool) -> int:
    added = 0
    with open(path, encoding="utf-8") as f:
        for line in f:
            c = line.rstrip("\n").split("\t")
            # P = a populated place. PPLX is a district inside a city
            # ("Manhattan"); the city is what a person would say. The rest are
            # abandoned, destroyed or seasonal places.
            if c[6] != "P" or c[7] in ("PPLX", "PPLQ", "PPLH", "PPLW", "PPLCH") or not c[8]:
                continue
            if c[0] in seen:
                continue
            people = int(c[14] or 0)
            if not villages_too and people <= 0 and not c[7].startswith("PPLA") and c[7] != "PPLC":
                continue
            seen.add(c[0])
            rows.append((c[8], c[1], f"{float(c[4]):.4f}", f"{float(c[5]):.4f}", str(people)))
            added += 1
    return added


print(f"{take(cities, False):7} from {os.path.basename(cities)}")
for extra in extras:
    print(f"{take(extra, True):7} more from {os.path.basename(extra)}")
rows.sort()
with gzip.open(os.path.join(out, "places.tsv.gz"), "wt", encoding="utf-8", compresslevel=9) as f:
    for row in rows:
        f.write("\t".join(row) + "\n")

names = []
with open(countries, encoding="utf-8") as f:
    for line in f:
        if line.startswith("#") or not line.strip():
            continue
        c = line.split("\t")
        names.append((c[0], c[4]))
with open(os.path.join(out, "countries.tsv"), "w", encoding="utf-8") as f:
    for code, name in sorted(names):
        f.write(f"{code}\t{name}\n")
print(f"{len(rows)} places, {len(names)} countries")
