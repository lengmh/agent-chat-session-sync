from __future__ import annotations

import json
from pathlib import Path
import tempfile
import tomllib
import unittest
from unittest.mock import patch

from agent_chat_session_sync.cli import provenance_checks
from agent_chat_session_sync.config import Settings
from agent_chat_session_sync.queue import EventDatabase
from agent_chat_session_sync.runtime import process_codex_hook


class RuntimeReceiptTests(unittest.TestCase):
    def settings(self, root: Path) -> Settings:
        return Settings(root / "data", root / "config.toml", root / "api.sock", root / ".codex")

    def test_hook_only_persists_receipt_and_logs_full_identity(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            settings = self.settings(root)
            event = {"hook_event_name": "SessionStart", "session_id": "temporary", "cwd": str(root)}
            process_codex_hook(event, settings, {})
            queued = EventDatabase(settings.database_path).list_events()
            self.assertEqual(len(queued), 1)
            self.assertEqual(queued[0].state, "received")
            log = settings.log_path.read_text(encoding="utf-8")
            for key in ("service_version=", "git_commit=", "package_path=", "python_path=", "config_path="):
                self.assertIn(key, log)

    def test_sqlite_failure_uses_fsynced_emergency_spool(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            settings = self.settings(root)
            event = {"hook_event_name": "Stop", "session_id": "temporary", "cwd": str(root)}
            with patch("agent_chat_session_sync.runtime.EventDatabase.enqueue", side_effect=OSError("disk busy")):
                process_codex_hook(event, settings, {})
            spool = settings.data_dir / "emergency-inbox.jsonl"
            record = json.loads(spool.read_text(encoding="utf-8"))
            self.assertEqual(record["raw"], event)


class ProvenanceTests(unittest.TestCase):
    def test_package_metadata_uses_runtime_version_as_single_source(self) -> None:
        root = Path(__file__).resolve().parents[1]
        project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertNotIn("version", project["project"])
        self.assertIn("version", project["project"]["dynamic"])
        self.assertEqual(
            project["tool"]["setuptools"]["dynamic"]["version"]["attr"],
            "agent_chat_session_sync.__version__",
        )

    def test_installed_version_mismatch_fails(self) -> None:
        checks = provenance_checks(
            "commit-a",
            "",
            "commit-a",
            {"git_commit": "commit-b", "package_path": "/pkg", "python_path": "/py"},
            {"git_commit": "commit-a", "package_path": "/pkg", "python_path": "/py"},
        )
        self.assertFalse(next(okay for name, okay, _ in checks if name == "built package commit"))

    def test_matching_clean_provenance_passes(self) -> None:
        identity = {"git_commit": "commit-a", "package_path": "/pkg", "python_path": "/py"}
        self.assertTrue(all(okay for _, okay, _ in provenance_checks("commit-a", "", "commit-a", identity, identity)))


if __name__ == "__main__":
    unittest.main()
