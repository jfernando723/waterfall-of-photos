#!/usr/bin/env bash
# Prove that an older Docker engine can load a saved Waterfall image file and
# run it. Synology's Container Manager runs Docker 24; an image saved by a
# newer engine is not guaranteed to load there, so it is tried.
#
#     bash ci/docker24.sh <file.tar> <image>
set -euo pipefail

file="${1:?usage: bash ci/docker24.sh <file.tar> <image>}"
image="${2:?usage: bash ci/docker24.sh <file.tar> <image>}"
dir="$(cd "$(dirname "$file")" && pwd)"

trap 'docker rm -f waterfall-old-engine >/dev/null 2>&1 || true' EXIT
docker rm -f waterfall-old-engine >/dev/null 2>&1 || true
docker run -d --privileged --name waterfall-old-engine -v "$dir:/work:ro" docker:24.0-dind >/dev/null
for _ in $(seq 1 30); do docker exec waterfall-old-engine docker info >/dev/null 2>&1 && break; sleep 2; done
docker exec waterfall-old-engine docker version --format 'engine {{.Server.Version}}'
docker exec waterfall-old-engine docker load -i "/work/$(basename "$file")"
docker exec waterfall-old-engine docker image inspect "$image" --format 'loaded: {{.RepoTags}} {{.Size}} bytes'
docker exec waterfall-old-engine docker run --rm --entrypoint python "$image" -c "print('it runs under Docker 24')"
