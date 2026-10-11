#!/usr/bin/env bash
# Run Waterfall from this checkout, for development.
#
#   bash scripts/dev.sh                                  this Mac only, port 7275
#   WATERFALL_BIND=0.0.0.0 bash scripts/dev.sh            let a phone on the LAN reach it
#                                                        (set WATERFALL_DAV_BIND=127.0.0.1 too, or
#                                                        the import door opens to the LAN as well)
#   WATERFALL_SIMULATE_LINK=275,1.5 bash scripts/dev.sh   pretend to be a slow, distant link
#   WATERFALL_DATA=.devdata/phone bash scripts/dev.sh     a different library
#
# The library is <data>/media/Visible and <data>/media/Hidden, exactly as in the
# container. The import door (WebDAV) is on WATERFALL_DAV_PORT, default 7276.
#
# Development passwords live in .devdata/dev.env, which is gitignored. Without
# that file, or with WATERFALL_NO_DEV_PASSWORDS=1, the first visit to a fresh
# data folder shows the set-up page instead, exactly as a fresh install does.
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONPATH="$PWD"
export WATERFALL_DATA="${WATERFALL_DATA:-$PWD/.devdata}"
export WATERFALL_BIND="${WATERFALL_BIND:-127.0.0.1}"
if [[ -f .devdata/dev.env && -z "${WATERFALL_NO_DEV_PASSWORDS:-}" ]]; then set -a; . .devdata/dev.env; set +a; fi
exec .venv/bin/python -m waterfall.app
