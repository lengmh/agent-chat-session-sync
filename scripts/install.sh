#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
VENV=${ACSS_VENV:-"$HOME/.local/share/agent-chat-session-sync/venv"}

COMMIT=$(git -C "$ROOT" rev-parse HEAD)
if [ -n "$(git -C "$ROOT" status --porcelain)" ]; then
  printf '%s\n' "refusing deployment: repository has uncommitted changes" >&2
  exit 1
fi

python3 -m venv "$VENV"
ACSS_BUILD_COMMIT="$COMMIT" "$VENV/bin/python" -m pip install --force-reinstall --no-cache-dir "$ROOT"
"$VENV/bin/agent-chat-session-sync" install-hooks
"$VENV/bin/agent-chat-session-sync" install-service
"$VENV/bin/agent-chat-session-sync" verify-install --source "$ROOT" --expected-commit "$COMMIT"
"$VENV/bin/agent-chat-session-sync" doctor
printf '%s\n' "installed and identity-verified commit $COMMIT; live E2E acceptance is still required"
