from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import time
import unittest

from agent_chat_session_sync.models import AgentEvent
from agent_chat_session_sync.resolver import CodexSessionResolver


def write_rollout(
    codex_home: Path,
    rollout_id: str,
    cwd: Path,
    prompt: str = "",
    turn_id: str = "",
    modified_at: float | None = None,
) -> Path:
    path = codex_home / "sessions/2026/07/29" / f"rollout-test-{rollout_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        {"type": "session_meta", "payload": {"id": rollout_id, "cwd": str(cwd)}},
    ]
    if prompt:
        lines.append(
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": prompt}],
                    "internal_chat_message_metadata_passthrough": {"turn_id": turn_id},
                },
            }
        )
    path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
    timestamp = time.time() if modified_at is None else modified_at
    os.utime(path, (timestamp, timestamp))
    return path


class ResolverTests(unittest.TestCase):
    def test_resolution_order_transcript_then_direct_session(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            home = root / ".codex"
            first = write_rollout(home, "019fab53-93d9-7032-aecf-29b5f9bcc362", root)
            second = write_rollout(home, "019fab54-93d9-7032-aecf-29b5f9bcc363", root)
            resolver = CodexSessionResolver(home, lambda _: None)
            event = AgentEvent("SessionStart", "019fab54-93d9-7032-aecf-29b5f9bcc363", str(root), str(first))
            self.assertEqual(resolver.resolve(event, time.time()).rollout_id, "019fab53-93d9-7032-aecf-29b5f9bcc362")
            event = AgentEvent("SessionStart", "019fab54-93d9-7032-aecf-29b5f9bcc363", str(root))
            result = resolver.resolve(event, time.time())
            self.assertEqual(result.rollout_path, str(second))
            self.assertEqual(result.method, "hook_session_id")

    def test_session_index_alias_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            home = root / ".codex"
            rollout_id = "019fab53-93d9-7032-aecf-29b5f9bcc362"
            write_rollout(home, rollout_id, root)
            (home / "session_index.jsonl").write_text(
                json.dumps({"id": rollout_id, "temporary_session_id": "temporary"}) + "\n", encoding="utf-8"
            )
            result = CodexSessionResolver(home, lambda _: None).resolve(
                AgentEvent("SessionStart", "temporary", str(root)), time.time()
            )
            self.assertEqual(result.method, "codex_state_mapping")

    def test_delayed_rollout_waits_then_resolves(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            home = root / ".codex"
            resolver = CodexSessionResolver(home, lambda _: None)
            event = AgentEvent("UserPromptSubmit", "temporary", str(root), prompt="hello")
            received = time.time()
            self.assertEqual(resolver.resolve(event, received).status, "pending")
            write_rollout(home, "019fab53-93d9-7032-aecf-29b5f9bcc362", root, "hello", "turn-1", received)
            result = resolver.resolve(event, received)
            self.assertEqual(result.status, "resolved")
            self.assertEqual(result.method, "multi_factor_correlation")
            self.assertEqual(result.turn_id, "turn-1")

    def test_same_directory_two_sessions_waits_for_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            home = root / ".codex"
            received = time.time()
            write_rollout(home, "019fab53-93d9-7032-aecf-29b5f9bcc362", root, modified_at=received)
            write_rollout(home, "019fab54-93d9-7032-aecf-29b5f9bcc363", root, modified_at=received)
            result = CodexSessionResolver(home, lambda _: None).resolve(
                AgentEvent("SessionStart", "temporary", str(root)), received
            )
            self.assertEqual(result.status, "waiting_confirmation")
            self.assertEqual(len(result.candidates), 2)

    def test_compaction_keeps_exact_transcript_identity(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            home = root / ".codex"
            path = write_rollout(home, "019fab53-93d9-7032-aecf-29b5f9bcc362", root)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"type": "compacted", "payload": {"message": "summary"}}) + "\n")
            result = CodexSessionResolver(home, lambda _: None).resolve(
                AgentEvent("Stop", "temporary", str(root), str(path), assistant_message="done"), time.time()
            )
            self.assertEqual(result.rollout_id, "019fab53-93d9-7032-aecf-29b5f9bcc362")


if __name__ == "__main__":
    unittest.main()
