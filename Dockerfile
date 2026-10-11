# syntax=docker/dockerfile:1
#
# Waterfall — a private scrolling feed for your own photos and videos.
#
# WHAT THIS IMAGE CAN REACH ON THE MACHINE IT RUNS ON: no folder, and one device.
#
# It is given no folder from the host at all. Photos and videos are copied INTO
# it through its own import door (a second port, admin password only), and live
# with everything else it makes in /data, a Docker named volume. With no path
# into any shared folder there is no permission to get wrong and nothing of
# yours that it could read, let alone alter.
#
# The one device is the machine's video chip (/dev/dri), which makes converting
# 4K video several times faster. It is a decoder, not storage. The image works
# without it and says so on its admin page.

# ------------------------------------------------------------------- base ---
FROM python:3.12-slim-trixie AS base

# Build options. The defaults are right for a Synology NAS and harmless
# anywhere else; a plain `docker build .` needs none of them.
#
#   WATERFALL_UID / WATERFALL_GID
#       The user the application runs as. With the named volume the compose
#       file uses, the number never has to match an account on the host. It
#       only matters if you replace that volume with a folder of your own.
#   VIDEO_GID
#       The group that owns /dev/dri on the host. 937 is "videodriver" on
#       Synology DSM 7. Elsewhere it is usually "render" (`getent group render`).
#       You do not have to rebuild for this: `group_add` in the compose file
#       does the same at run time, and the admin page tells you the number.
#   INTEL_DRIVERS
#       full   both Intel video drivers, the newer one with every feature
#              (Debian keeps that build in its "non-free" section: it is under
#              the MIT licence, but some of its parts come without source)
#       free   both drivers, the newer one built from source only
#       none   no video chip support; everything is converted in software
#       Ignored on anything but x86-64, where there is no Intel chip to drive.
ARG WATERFALL_UID=1027
ARG WATERFALL_GID=1027
ARG VIDEO_GID=937
ARG INTEL_DRIVERS=full

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# ffmpeg does the video work; exiftool reads phone metadata (HEIC EXIF and the
# QuickTime keys inside MOV). Both from Debian, so they are patched by Debian.
#
# The va packages are what let ffmpeg use an Intel video chip. Two drivers are
# installed because either can serve chips of the Gemini Lake generation and
# its neighbours, and which one works is only known on the machine; Waterfall
# tries both and checks the picture before trusting either.
#
# The last line records exactly what was installed. THIRD-PARTY-NOTICES.md
# points at it: it is the list anyone needs to fetch the source of the GPL
# parts from Debian.
RUN set -eu; \
    drivers=""; \
    if [ "$(dpkg --print-architecture)" = "amd64" ]; then \
      case "$INTEL_DRIVERS" in \
        full) sed -i 's/^Components: main$/Components: main contrib non-free non-free-firmware/' \
                  /etc/apt/sources.list.d/debian.sources; \
              drivers="intel-media-va-driver-non-free i965-va-driver vainfo" ;; \
        free) drivers="intel-media-va-driver i965-va-driver vainfo" ;; \
        none) ;; \
        *)    echo "INTEL_DRIVERS must be full, free or none, not '$INTEL_DRIVERS'" >&2; exit 1 ;; \
      esac; \
    fi; \
    apt-get update; \
    apt-get install -y --no-install-recommends ffmpeg libimage-exiftool-perl $drivers; \
    rm -rf /var/lib/apt/lists/*; \
    mkdir -p /app; \
    dpkg-query -W -f='${binary:Package}\t${Version}\t${source:Package}\t${source:Version}\n' \
        > /app/DEBIAN-PACKAGES.tsv

# A fixed non-root user. It is put in the group that owns the video chip, so
# that the chip can be used without the container being root.
RUN groupadd -g "$WATERFALL_GID" waterfall \
 && useradd -u "$WATERFALL_UID" -g "$WATERFALL_GID" -M -s /usr/sbin/nologin waterfall \
 && groupadd -o -g "$VIDEO_GID" videodriver && usermod -aG videodriver waterfall

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && pip freeze > /app/PYTHON-PACKAGES.txt
COPY waterfall/ ./waterfall/
# What else is in this image and on what terms. LICENSE is Waterfall's own; it
# is matched with a star because a private build may not have chosen one.
COPY THIRD-PARTY-NOTICES.md LICENSE* ./

# ------------------------------------------------------------------- test ---
# Prove the converter works with THIS image's ffmpeg before an image exists.
# The suite makes its own stand-in phone media (HEIC, portrait HDR HEVC, a
# Live Photo pair), converts it, drives the import door, and checks that
# nothing under Hidden/ can be reached without the admin password. It runs as
# the ordinary user on purpose: root ignores file permissions, so a read-only
# folder would prove nothing to root.
FROM base AS test
COPY tests/ ./tests/
USER waterfall
ENV HOME=/tmp
RUN python -m unittest discover -s tests && date -u > /tmp/tested-at

# ---------------------------------------------------------------- runtime ---
FROM base AS runtime

# Filled in by whoever builds a copy to hand to other people. A private build
# leaves them alone.
ARG IMAGE_SOURCE=""
ARG IMAGE_LICENSES="NONE"
LABEL org.opencontainers.image.title="Waterfall" \
      org.opencontainers.image.description="A private scrolling feed for your own photos and videos, made for a slow link." \
      org.opencontainers.image.source="${IMAGE_SOURCE}" \
      org.opencontainers.image.licenses="${IMAGE_LICENSES}"

# Copying this file is what forces the test stage to run: BuildKit skips any
# stage nothing depends on, and a skipped test is a passed test.
COPY --from=test /tmp/tested-at /app/tested-at

ENV WATERFALL_DATA=/data \
    WATERFALL_PORT=7275 \
    WATERFALL_DAV_PORT=7276 \
    HOME=/tmp

# /data is created owned by the application user, so that the named volume
# Docker makes from it starts out writable by that user.
RUN mkdir -p /data && chown waterfall:waterfall /data

USER waterfall
EXPOSE 7275 7276

# Deliberately NO `VOLUME`: Docker would invent an anonymous volume when none
# is given, and the whole library would silently vanish the next time the
# container was recreated.

# Proves the index really opens and answers, not merely that a process is up.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import json,os,sys,urllib.request; \
d=json.load(urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('WATERFALL_PORT','7275'),timeout=4)); \
sys.exit(0 if d.get('ok') else 1)"

CMD ["python", "-m", "waterfall.app"]
