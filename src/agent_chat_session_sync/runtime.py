from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import time
from typing import Any

from .agents.codex import CodexAdapter
from .config import Settings
from .provenance import current_provenance
from .queue import EventDatabase, canonical_json


def make_logger(path: Path):
    def log(message: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {message}\n")

    return log


def process_codex_hook(raw: dict[str, Any], settings: Settings | None = None, environment: dict[str, str] | None = None) -> None:
    settings = settings or Settings.from_env()
    environment = dict(os.environ) if environment is None else environment
    logger = make_logger(settings.log_path)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    provenance = current_provenance()
    identity = (
        f"service_version={provenance.service_version} git_commit={provenance.git_commit} "
        f"package_path={provenance.package_path} python_path={provenance.python_path} "
        f"config_path={settings.cc_config}"
    )
    bridge_originated = CodexAdapter.bridge_originated(environment)
    try:
        event_id, created = EventDatabase(settings.database_path).enqueue(raw, bridge_originated)
        logger(
            f"hook receipt inbox={event_id} created={str(created).lower()} "
            f"event={raw.get('hook_event_name')} session={raw.get('session_id')} {identity}"
        )
    except Exception as exc:  # A disk/SQLite failure falls back to an fsynced append-only spool.
        spool = settings.data_dir / "emergency-inbox.jsonl"
        document = canonical_json(
            {"raw": raw, "bridge_originated": bridge_originated, "received_at": time.time()}
        ) + "\n"
        fd = os.open(spool, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, document.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        logger(f"hook emergency_spool={spool} sqlite_error={exc} {identity}")


def hook_main() -> int:
    try:
        raw = json.load(sys.stdin)
    except json.JSONDecodeError:
        print("{}")
        return 0
    process_codex_hook(raw)
    print("{}")
    return 0
