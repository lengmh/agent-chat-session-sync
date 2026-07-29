from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
import sqlite3

from agent_chat_session_sync.models import Binding
from agent_chat_session_sync.queue import EventDatabase, stable_event_id


class EventDatabaseTests(unittest.TestCase):
    def test_hook_duplicate_is_one_durable_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            database = EventDatabase(Path(raw) / "events.sqlite3")
            event = {"hook_event_name": "Stop", "session_id": "temporary", "last_assistant_message": "done"}
            first, created = database.enqueue(event, now=100)
            second, duplicate_created = database.enqueue(event, now=101)
            self.assertTrue(created)
            self.assertFalse(duplicate_created)
            self.assertEqual(first, second)
            self.assertEqual(database.get_event(first).state, "received")

    def test_outbox_survives_reopen_and_records_platform_message(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "events.sqlite3"
            database = EventDatabase(path)
            inbox_id, _ = database.enqueue({"hook_event_name": "Stop"}, now=100)
            event_id = stable_event_id("rollout", "Stop", "turn", "answer")
            database.ensure_outbox(event_id, inbox_id, "rollout", "Stop", "answer")
            database.mark_outbox_sending(event_id)

            reopened = EventDatabase(path)
            self.assertEqual(reopened.get_outbox(event_id).status, "sending")
            reopened.mark_outbox_delivered(event_id, "om_123")
            delivered = reopened.get_outbox(event_id)
            self.assertEqual(delivered.status, "delivered")
            self.assertEqual(delivered.platform_message_id, "om_123")

    def test_stable_event_id_uses_turn_to_distinguish_repeated_content(self) -> None:
        first = stable_event_id("rollout", "UserPromptSubmit", "turn-1", "again")
        duplicate = stable_event_id("rollout", "UserPromptSubmit", "turn-1", "again")
        second_turn = stable_event_id("rollout", "UserPromptSubmit", "turn-2", "again")
        self.assertEqual(first, duplicate)
        self.assertNotEqual(first, second_turn)

    def test_claude_identity_is_namespaced_from_codex(self) -> None:
        raw = {"hook_event_name": "Stop", "session_id": "same"}
        with tempfile.TemporaryDirectory() as directory:
            database = EventDatabase(Path(directory) / "events.sqlite3")
            codex, _ = database.enqueue(raw, agent_type="codex")
            claude, _ = database.enqueue(raw, agent_type="claudecode")
            self.assertNotEqual(codex, claude)
        self.assertNotEqual(
            stable_event_id("same", "Stop", "turn", "answer", "codex"),
            stable_event_id("same", "Stop", "turn", "answer", "claudecode"),
        )

    def test_schema_v1_database_adds_agent_type(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "events.sqlite3"
            db = sqlite3.connect(path)
            db.execute("CREATE TABLE inbox (id INTEGER PRIMARY KEY, receipt_id TEXT, state TEXT, raw_json TEXT, bridge_originated INTEGER, received_at REAL, updated_at REAL, next_attempt_at REAL, attempts INTEGER, rollout_id TEXT, rollout_path TEXT, resolution_method TEXT, candidates_json TEXT, stable_event_id TEXT, last_error TEXT)")
            db.commit()
            db.close()
            EventDatabase(path)
            reopened = sqlite3.connect(path)
            columns = {row[1] for row in reopened.execute("PRAGMA table_info(inbox)")}
            reopened.close()
            self.assertIn("agent_type", columns)

    def test_legacy_bindings_are_imported_once(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            state = root / "state.json"
            binding = Binding("oc_1", "feishu:oc_1:ou_1", "p", "/work", 1, "now")
            state.write_text(json.dumps({"sessions": {"rollout": binding.to_dict()}}), encoding="utf-8")
            database = EventDatabase(root / "events.sqlite3")
            self.assertEqual(database.import_legacy_bindings(state), 1)
            self.assertEqual(database.import_legacy_bindings(state), 0)
            self.assertEqual(database.get_binding("rollout"), binding)

    def test_list_bindings_preserves_namespaced_keys(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            database = EventDatabase(Path(raw) / "events.sqlite3")
            binding = Binding("oc_1", "feishu:oc_1:ou_1", "claude", "/work", 1, "now")
            database.put_binding("claudecode:session", binding)
            self.assertEqual(database.list_bindings(), [("claudecode:session", binding)])


if __name__ == "__main__":
    unittest.main()
