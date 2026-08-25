#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
REVISION=5d4c96dd12774574369e75b60084140101c9a59a
SOURCE_DIR=${1:-"$ROOT/.build/cc-connect"}
OUTPUT=${2:-"$ROOT/dist/cc-connect"}

if [ ! -d "$SOURCE_DIR/.git" ]; then
  git clone https://github.com/chenhg5/cc-connect.git "$SOURCE_DIR"
fi
git -C "$SOURCE_DIR" fetch origin "$REVISION"
git -C "$SOURCE_DIR" checkout --detach "$REVISION"
git -C "$SOURCE_DIR" apply --check "$ROOT/patches/cc-connect-v1.4.1-bind-agent.patch"
git -C "$SOURCE_DIR" apply "$ROOT/patches/cc-connect-v1.4.1-bind-agent.patch"
git -C "$SOURCE_DIR" apply --check "$ROOT/patches/cc-connect-v1.4.1-binding-routing.patch"
git -C "$SOURCE_DIR" apply "$ROOT/patches/cc-connect-v1.4.1-binding-routing.patch"
git -C "$SOURCE_DIR" apply --check "$ROOT/patches/cc-connect-v1.4.1-rollout-refresh.patch"
git -C "$SOURCE_DIR" apply "$ROOT/patches/cc-connect-v1.4.1-rollout-refresh.patch"
git -C "$SOURCE_DIR" apply --check "$ROOT/patches/cc-connect-v1.4.1-windows-local-endpoint.patch"
git -C "$SOURCE_DIR" apply "$ROOT/patches/cc-connect-v1.4.1-windows-local-endpoint.patch"
mkdir -p "$(dirname -- "$OUTPUT")"
# Upstream attachment CUJs leave asynchronous cleanup work that is flaky under
# parallel package scheduling on macOS. A single test scheduler keeps the
# release gate deterministic; the produced daemon retains normal concurrency.
(cd "$SOURCE_DIR" && GOMAXPROCS=1 go test ./core ./agent/codex ./agent/claudecode ./platform/feishu && go build -tags 'no_web goolm' -o "$OUTPUT" ./cmd/cc-connect)
printf '%s\n' "built $OUTPUT"
