#!/usr/bin/env bash
# Start a Waterfall image the way docker-compose.yml starts it, and prove it
# works: hardened, importing through the door and from the page, hiding what
# is hidden, keeping a Live Photo's clip, saying in its log what it refused,
# and keeping its library when the container is replaced.
#
#     bash ci/prove.sh <image>
#
# EXPECT_DRIVERS=no   for an image built without the Intel video drivers
# EXPECT_UID=1027     the user the image should run as
# EXPECT_LICENSE=no   for a private build that carries no LICENSE file
#
# Needs docker, curl and python3. Uses ports 7275 and 7276 on this machine and
# removes everything it made, whether it passes or not.
set -euo pipefail

image="${1:?usage: bash ci/prove.sh <image>}"
name=waterfall-proof
volume=waterfall-proof-data
base=http://127.0.0.1:7275
door=http://127.0.0.1:7276
password=ci-admin-password
work="$(mktemp -d)"
drivers="${EXPECT_DRIVERS:-$([[ "$(docker image inspect -f '{{.Architecture}}' "$image")" == amd64 ]] && echo yes || echo no)}"

cleanup() {
  docker rm -f "$name" >/dev/null 2>&1 || true
  docker volume rm "$volume" >/dev/null 2>&1 || true
  rm -rf "$work"
}
trap cleanup EXIT
cleanup; mkdir -p "$work"

say() { printf '\n== %s\n' "$*"; }
code() { curl -s -o /dev/null -w '%{http_code}' "$@"; }
field() { python3 -c 'import json,sys; print(json.load(sys.stdin).get(sys.argv[1]))' "$1"; }
start() {
  docker run -d --name "$name" --read-only --tmpfs /tmp --cap-drop ALL \
    --security-opt no-new-privileges --memory 4g --pids-limit 512 \
    -v "$volume":/data -e TZ=Etc/UTC \
    -p 127.0.0.1:7275:7275 -p 127.0.0.1:7276:7276 "$image" >/dev/null
  for _ in $(seq 1 60); do
    [[ "$(docker inspect -f '{{.State.Health.Status}}' "$name")" == healthy ]] && return 0
    sleep 2
  done
  docker logs "$name"; echo "the container never became healthy" >&2; return 1
}
picture() {     # a stand-in photo made by the image's own Pillow: no real media anywhere
  docker run --rm --entrypoint python "$image" -c \
    "import sys; from PIL import Image; Image.new('RGB', ($1), ($2)).save(sys.stdout.buffer, 'JPEG')" > "$3"
}

start

say "it is not root, its own filesystem is read-only, and it has no capabilities"
test "$(docker exec "$name" id -u)" = "${EXPECT_UID:-1027}"
! docker exec "$name" sh -c 'touch /app/probe' 2>/dev/null
docker exec "$name" sh -c 'grep -q "^CapEff:.0000000000000000$" /proc/1/status'

say "the only mount is Docker's own volume"
docker inspect -f '{{range .Mounts}}{{.Type}} {{.Destination}}{{"\n"}}{{end}}' "$name" | grep -v '^$' | tee "$work/mounts.txt"
test "$(grep -c . "$work/mounts.txt")" = "1" && grep -qx "volume /data" "$work/mounts.txt"

say "the notices and the lists of what is installed are inside it"
docker exec "$name" sh -c 'test -s /app/THIRD-PARTY-NOTICES.md && test -s /app/waterfall/data/ATTRIBUTION.txt \
  && test -s /app/PYTHON-PACKAGES.txt && grep -q "^ffmpeg	" /app/DEBIAN-PACKAGES.tsv \
  && test -s /usr/share/doc/ffmpeg/copyright && test -s /usr/share/doc/libimage-exiftool-perl/copyright'
if [[ "${EXPECT_LICENSE:-yes}" == yes ]]; then docker exec "$name" test -s /app/LICENSE; fi

say "before set-up: the import door refuses, and a stranger is told nothing"
test "$(code -X PROPFIND "$door/")" = "503"
curl -fsS "$base/healthz" | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d["ok"] and set(d) == {"ok", "version"}, d'
curl -fsS -D "$work/headers.txt" -o /dev/null "$base/"
grep -qi "^content-security-policy: default-src 'self'" "$work/headers.txt"
grep -qi "^x-frame-options: DENY" "$work/headers.txt"

say "set-up, then one photo into each wall through the door"
curl -fsS -c "$work/jar" -H 'Content-Type: application/json' -H 'X-Waterfall: 1' \
  -d "{\"admin\":\"$password\"}" "$base/api/setup" >/dev/null
test "$(code -X POST -H 'X-Waterfall: 1' -H 'Content-Type: application/json' -d '{"admin":"someone-else"}' "$base/api/setup")" = "409"
test "$(code -X PROPFIND "$door/")" = "401"
picture "1600,1200" "40,90,160" "$work/one.jpg"
picture "1200,1600" "160,60,40" "$work/two.jpg"
curl -fsS -u "x:$password" -X MKCOL "$door/Visible/Trip"
curl -fsS -u "x:$password" -X MKCOL "$door/Hidden/Secret"
curl -fsS -u "x:$password" -T "$work/one.jpg" "$door/Visible/Trip/one.jpg"
curl -fsS -u "x:$password" -T "$work/two.jpg" "$door/Hidden/Secret/two.jpg"
curl -fsS -b "$work/jar" -H 'X-Waterfall: 1' -X POST "$base/api/admin/rescan" >/dev/null
for _ in $(seq 1 30); do
  [[ "$(curl -fsS -b "$work/jar" "$base/healthz" | field ready)" == "2" ]] && break; sleep 2
done
test "$(curl -fsS -b "$work/jar" "$base/healthz" | field ready)" = "2"

say "the full-size files are gone and a stand-in is left for each"
curl -fsS -u "x:$password" -X PROPFIND -H 'Depth: 1' "$door/Visible/Trip/" > "$work/listing.xml"
grep -q 'one.jpg.waterfall.txt' "$work/listing.xml"
! grep -q 'Trip/one.jpg<' "$work/listing.xml"

say "no video chip here, so it must say so and convert in software"
curl -fsS -b "$work/jar" "$base/api/admin/status" | python3 -c '
import json, sys
h = json.load(sys.stdin)["hardware"]
print(h["path"], "|", h["why"])
assert not h["in_use"], h'
if [[ "$drivers" == yes ]]; then
  say "the video drivers are in the image for when there is a chip"
  docker exec "$name" sh -c 'ls /usr/lib/x86_64-linux-gnu/dri/ | grep iHD_drv_video && ls /usr/lib/x86_64-linux-gnu/dri/ | grep i965_drv_video'
else
  say "this image carries no Intel video driver, as asked"
  ! docker exec "$name" sh -c 'ls /usr/lib/*/dri/ 2>/dev/null | grep -E "iHD_drv_video|i965_drv_video"'
fi
docker exec "$name" id

say "with no password: one item, and nothing hidden"
test "$(curl -fsS "$base/api/feed" | python3 -c 'import json,sys; print(len(json.load(sys.stdin)["items"]))')" = "1"
test "$(code "$base/api/feed?realm=hidden")" = "403"
test "$(curl -fsS "$base/healthz" | field ready)" = "None"

say "with the admin password: the hidden wall"
test "$(curl -fsS -b "$work/jar" "$base/api/feed?realm=hidden" | python3 -c 'import json,sys; print(len(json.load(sys.stdin)["items"]))')" = "1"

say "uploading from the page: one photo, in two pieces, and only for the admin"
picture "900,1200" "30,140,90" "$work/three.jpg"
size="$(wc -c < "$work/three.jpg" | tr -d ' ')"; half=$((size / 2))
head -c "$half" "$work/three.jpg" > "$work/three.a"
tail -c +"$((half + 1))" "$work/three.jpg" > "$work/three.b"
piece() {
  curl -fsS -b "$work/jar" -H 'X-Waterfall: 1' -H 'Content-Type: application/octet-stream' \
    -H 'X-Upload-Path: Phone%2Fthree.jpg' -H 'X-Upload-Id: 00112233445566ff' \
    -H "X-Upload-Total: $size" -H "X-Upload-Offset: $1" --data-binary "@$2" "$base/api/admin/upload?realm=visible"
}
test "$(piece 0 "$work/three.a" | field done)" = "False"
test "$(piece "$half" "$work/three.b" | field name)" = "three.jpg"
test "$(code -X POST -H 'X-Waterfall: 1' -H 'X-Upload-Path: x.jpg' -H 'X-Upload-Id: 00112233445566ff' \
  -H 'X-Upload-Total: 3' -H 'X-Upload-Offset: 0' --data-binary abc "$base/api/admin/upload?realm=visible")" = "403"

say "a Live Photo: a still and its clip, copied in together, are one item that moves"
picture "1200,1600" "150,40,120" "$work/live.jpg"
docker run --rm --entrypoint sh "$image" -c 'ffmpeg -nostdin -loglevel error -f lavfi \
  -i testsrc2=size=640x480:rate=30 -t 2 -c:v libx264 -pix_fmt yuv420p /tmp/clip.mov && cat /tmp/clip.mov' > "$work/live.mov"
curl -fsS -u "x:$password" -T "$work/live.jpg" "$door/Visible/Trip/IMG_1.jpg"
curl -fsS -u "x:$password" -T "$work/live.mov" "$door/Visible/Trip/IMG_1.MOV"
curl -fsS -b "$work/jar" -H 'X-Waterfall: 1' -X POST "$base/api/admin/rescan" >/dev/null
tally() {
  curl -fsS -b "$work/jar" "$base/api/admin/status" | python3 -c '
import json, sys
c = json.load(sys.stdin)["counts"]
print(c["ready"], c["live"], c["waiting"], c["paired"], c["failed"])'
}
for _ in $(seq 1 60); do [[ "$(tally)" == "4 1 0 0 0" ]] && break; sleep 2; done
test "$(tally)" = "4 1 0 0 0"
key="$(curl -fsS "$base/api/feed?limit=100" | python3 -c 'import json,sys; print([i["key"] for i in json.load(sys.stdin)["items"] if i.get("lv")][0])')"
pipeline="$(curl -fsS "$base/api/me" | field pipeline)"
test "$(curl -s -o /dev/null -w '%{http_code} %{content_type}' "$base/m/$pipeline/$key/live.mp4")" = "200 video/mp4"

say "the log says what the door refused, and names no file or folder"
test "$(code -u "x:not-the-password" -X PROPFIND "$door/")" = "401"
docker logs "$name" > "$work/log.txt" 2>&1
grep -q "import door: PROPFIND answered 401, wrong password" "$work/log.txt"
! grep -qE "one\.jpg|two\.jpg|three\.jpg|IMG_1|Trip|Secret|Phone" "$work/log.txt"

say "it survives being replaced: the library is in the volume, not the container"
docker rm -f "$name" >/dev/null
start
test "$(curl -fsS -b "$work/jar" "$base/healthz" | field ready)" = "4"

say "all of it held"
