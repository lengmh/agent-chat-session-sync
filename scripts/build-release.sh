#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
DIST=${1:-"$ROOT/dist"}

if [ -n "$(git -C "$ROOT" status --porcelain)" ]; then
  printf '%s\n' "refusing release build: repository has uncommitted changes" >&2
  exit 1
fi

COMMIT=$(git -C "$ROOT" rev-parse HEAD)
ACSS_BUILD_COMMIT="$COMMIT" python3 -m build --outdir "$DIST" "$ROOT"
python3 "$ROOT/scripts/write-checksums.py" "$DIST"
python3 "$ROOT/scripts/verify-release-artifacts.py" "$DIST"
printf '%s\n' "release artifacts verified for commit $COMMIT"
