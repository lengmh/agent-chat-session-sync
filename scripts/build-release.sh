#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
DIST=${1:-"$ROOT/dist"}
WINDOWS_EXE=${ACSS_WINDOWS_EXE:-}

if [ -n "$(git -C "$ROOT" status --porcelain)" ]; then
  printf '%s\n' "refusing release build: repository has uncommitted changes" >&2
  exit 1
fi

if [ -e "$DIST" ] || [ -L "$DIST" ]; then
  printf '%s\n' "refusing existing release directory: $DIST" >&2
  exit 1
fi
if [ -z "$WINDOWS_EXE" ] || [ ! -f "$WINDOWS_EXE" ]; then
  printf '%s\n' "ACSS_WINDOWS_EXE must name a prebuilt Windows x64 executable" >&2
  exit 1
fi
if ! command -v uv >/dev/null 2>&1; then
  printf '%s\n' "uv is required for release builds" >&2
  exit 1
fi

COMMIT=$(git -C "$ROOT" rev-parse HEAD)
mkdir "$DIST"
ACSS_BUILD_COMMIT="$COMMIT" uv run --locked --extra dev python -m build --outdir "$DIST" "$ROOT"
cp -- "$WINDOWS_EXE" "$DIST/cc-connect-windows-x64.exe"
uv run --locked python "$ROOT/scripts/write-checksums.py" "$DIST"
ACSS_EXPECTED_COMMIT="$COMMIT" uv run --locked python "$ROOT/scripts/verify-release-artifacts.py" "$DIST"
printf '%s\n' "release artifacts verified for commit $COMMIT"
