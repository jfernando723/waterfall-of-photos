# Third-party notices

Waterfall's own code is covered by the `LICENSE` file beside this one. A Waterfall
container image, once you have built one or been given one, also contains other
people's software and data, each under its own licence. This file says what is in
such an image, on what terms, and where its source is.

It is written in good faith by people who are not lawyers. If something here is wrong
or missing, please open an issue.

## The short version

| | |
|---|---|
| **The image as a whole** | Contains programs under the GNU General Public License (ffmpeg with x264 and x265, among others). You may run it and pass it on; if you pass it on, the GPL's terms come with those parts, including saying where their source is, as below. |
| **Waterfall's own code** | Under its own licence (`LICENSE`), which is compatible with the GPL parts it runs beside. |
| **Place names** | From GeoNames, Creative Commons Attribution 4.0. Credit is given here, in `waterfall/data/ATTRIBUTION.txt` inside the image, and on the settings sheet in the app. |
| **Exactly what is installed** | Every image carries two lists made while it was built: `/app/DEBIAN-PACKAGES.tsv` (each Debian package, its version, and the source package it was built from) and `/app/PYTHON-PACKAGES.txt`. Each release publishes the same two files beside the image. |

## Source code for the GPL and LGPL parts

The image is built on Debian. Its GPL and LGPL programs are Debian's own unmodified
packages, installed by `apt-get` while the image is built, and the two encoders bundled
inside the `pillow_heif` wheel.

- **Debian packages.** `/app/DEBIAN-PACKAGES.tsv` names the exact source package and
  version of every package in the image. Debian keeps every version it has ever
  published at <https://snapshot.debian.org/>, and the current ones at
  <https://packages.debian.org/> ("Download Source Package" on each package's page).
  Every package's own copyright and licence text is in the image at
  `/usr/share/doc/<package>/copyright`.
- **`pillow_heif`.** Its wheels bundle libheif, libde265 and x265. The versions and
  the addresses of their source are in the wheel's own
  `pillow_heif-<version>.dist-info/licenses/LICENSES_bundled.txt`, which is in the image
  under `/usr/local/lib/python3.12/site-packages/`.
- **The exact address of each.** For any line of `/app/DEBIAN-PACKAGES.tsv`, the source
  is at `https://snapshot.debian.org/package/<third column>/<fourth column>/`. For
  example, an image whose list says `ffmpeg  7:7.1.5-0+deb13u1  ffmpeg  7:7.1.5-0+deb13u1`
  was built from <https://snapshot.debian.org/package/ffmpeg/7%3A7.1.5-0%2Bdeb13u1/>.

Those directions are how the source is offered: from Debian's own servers, free of
charge, for exactly the versions in the image. The image's publisher keeps no second
copy and provides no support.

Waterfall does not modify any of these programs. It runs `ffmpeg` and `exiftool` as
separate programs, and loads the image libraries through Pillow.

## What is in the image

### Installed by the Dockerfile

| Component | What it is for | Licence |
|---|---|---|
| **ffmpeg** (Debian package `ffmpeg` and its libraries) | Converts video | Debian builds it with GPL parts enabled, so the programs are under **GPL-2.0-or-later**. It links, among others, **x264** (GPL-2.0-or-later) and **x265** (GPL-2.0-or-later). |
| **ExifTool** (`libimage-exiftool-perl`) | Reads when, where and with what a photo or video was taken | The same terms as Perl: **GPL-1.0-or-later or the Artistic License**, at your choice |
| **Intel Media Driver** (`intel-media-va-driver-non-free`; x86-64 images only) | Lets ffmpeg use an Intel video chip | **MIT**, with parts under **BSD-3-Clause**. Debian keeps this build in its "non-free" section for one reason: some of its pre-built shader programs ("media kernels") are published by Intel without their source, so Debian cannot rebuild them. The licence itself allows copying and passing on. An image built with `INTEL_DRIVERS=free` uses Debian's source-only build of the same driver instead, and one built with `INTEL_DRIVERS=none` has neither driver. |
| **Intel VA-API driver** (`i965-va-driver`; x86-64 images only) | The older driver for the same chips | **MIT** |
| **libva** and `vainfo` (x86-64 images only) | The interface both drivers plug into | **MIT** |

### Python packages (`requirements.txt`)

| Package | Licence |
|---|---|
| Flask, Werkzeug, Jinja2, MarkupSafe, itsdangerous, click | BSD-3-Clause |
| blinker | MIT |
| waitress | Zope Public License 2.1 |
| Pillow | MIT-CMU (also called HPND). Its wheels bundle libjpeg-turbo, libpng, libtiff, libwebp, OpenJPEG, FreeType, HarfBuzz, Raqm, Little CMS, libavif, aom, dav1d, libyuv, Brotli, bzip2, liblzma and zlib, all under permissive licences. The full texts are in the image at `pillow-<version>.dist-info/licenses/LICENSE`. |
| pillow_heif | Its own code is BSD-3-Clause. **Its wheels are, in its authors' words, under GPLv2 "due to base library licenses"**: they bundle **libheif** (LGPL-3.0), **libde265** (LGPL-3.0) and **x265** (GPL-2.0-or-later). Waterfall only reads HEIC photos, which uses libheif and libde265; x265 is in the wheel but Waterfall never calls it. |

### The base image

| Component | Licence |
|---|---|
| **Python 3.12** (the official `python:3.12-slim-trixie` image) | Python Software Foundation License 2.0 |
| **Debian 13 "trixie"**, the minimal system underneath | Many packages under many licences, most of them GPL, LGPL, BSD or MIT. Each one's terms are in the image at `/usr/share/doc/<package>/copyright`. |

### Data

| Data | Source and terms |
|---|---|
| `waterfall/data/places-preferred.tsv` | A hand-made list. Its positions were checked against **OpenStreetMap** (data © OpenStreetMap contributors, Open Database Licence, <https://www.openstreetmap.org/copyright>), GeoNames and Wikipedia, and its notes say which. |
| `waterfall/data/places.tsv.gz`, `waterfall/data/countries.tsv` | Derived from the **GeoNames** geographical database, <https://www.geonames.org/>, licensed **Creative Commons Attribution 4.0** (<https://creativecommons.org/licenses/by/4.0/>). What was changed is listed in `waterfall/data/ATTRIBUTION.txt`. GeoNames provides the data as is, without warranty, and does not endorse Waterfall. |

## Patents

Waterfall converts to and from H.264, HEVC and AAC. Those formats are covered by
patents in some countries. The software that does the work here (ffmpeg, x264, x265,
libde265) is free software and is distributed in this form by Debian and by many other
projects, but none of those licences grants a patent licence for the formats
themselves. If you intend to do more than run Waterfall for yourself, that is yours to
look into.

## Not in the image

The development helper `imageio-ffmpeg` (BSD-2-Clause, in `requirements-dev.txt`)
carries a static ffmpeg build for working on Waterfall on a machine without one. It is
never installed in the image.
