from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import time
from typing import Any

from .config import Settings
from .provenance import current_provenance
from .queue import EventDatabase, canonical_json
from .security import ensure_private_directory, harden_private_file


def make_logger(path: Path):
    def log(message: str) -> None:
        ensure_private_directory(path.parent)
        with path.open("a", encoding="utf-8") as handle:
            harden_private_file(path)
            handle.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {message}\n")

    return log


def process_agent_hook(
    raw: dict[str, Any],
    agent_type: str,
    settings: Settings | None = None,
    environment: dict[str, str] | None = None,
) -> None:
    settings = settings or Settings.from_env()
    environment = dict(os.environ) if environment is None else environment
    logger = make_logger(settings.log_path)
    ensure_private_directory(settings.data_dir)
    provenance = current_provenance()
    identity = (
        f"service_version={provenance.service_version} git_commit={provenance.git_commit} "
        f"package_path={provenance.package_path} python_path={provenance.python_path} "
        f"config_path={settings.cc_config}"
    )
    bridge_originated = bool(environment.get("CC_SESSION_KEY") or environment.get("ACSS_BRIDGE_ORIGIN"))
    try:
        event_id, created = EventDatabase(settings.database_path).enqueue(
            raw, bridge_originated, agent_type=agent_type
        )
        logger(
            f"hook receipt inbox={event_id} created={str(created).lower()} "
            f"agent_type={agent_type} event={raw.get('hook_event_name')} "
            f"session={raw.get('session_id')} {identity}"
        )
    except Exception as exc:  # A disk/SQLite failure falls back to an fsynced append-only spool.
        spool = settings.data_dir / "emergency-inbox.jsonl"
        document = canonical_json(
            {
                "raw": raw,
                "bridge_originated": bridge_originated,
                "agent_type": agent_type,
                "received_at": time.time(),
            }
        ) + "\n"
        fd = os.open(spool, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, document.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        logger(f"hook emergency_spool={spool} sqlite_error={exc} {identity}")


def process_codex_hook(
    raw: dict[str, Any],
    settings: Settings | None = None,
    environment: dict[str, str] | None = None,
) -> None:
    process_agent_hook(raw, "codex", settings, environment)


def hook_main(agent_type: str = "codex") -> int:
    try:
        raw = json.load(sys.stdin)
    except json.JSONDecodeError:
        print("{}")
        return 0
    process_agent_hook(raw, agent_type)
    print("{}")
    return 0
