from pathlib import Path
import json
import os
import tempfile
import time
import unittest

from agent_chat_session_sync.agents.codex import CodexAdapter


class CodexAdapterTests(unittest.TestCase):
    @staticmethod
    def write_rollout(codex_home: Path, session_id: str, cwd: Path, age_seconds: int = 0) -> Path:
        transcript = codex_home / "sessions" / f"rollout-2026-07-29T00-00-00-{session_id}.jsonl"
        transcript.parent.mkdir(parents=True, exist_ok=True)
        transcript.write_text(
            json.dumps(
                {
                    "type": "session_meta",
                    "payload": {"id": session_id, "session_id": session_id, "cwd": str(cwd)},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        timestamp = time.time() - age_seconds
        os.utime(transcript, (timestamp, timestamp))
        return transcript

    def test_transcript_rollout_wins_over_temporary_session_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tmp_path = Path(directory)
            rollout_id = "019f92ba-d95e-7e02-b81b-ddee818481b9"
            transcript = tmp_path / f"rollout-2026-07-27T00-00-00-{rollout_id}.jsonl"
            transcript.write_text("{}\n", encoding="utf-8")
            event = CodexAdapter.parse_event(
                {
                    "session_id": "019fa3f3-e96d-7df2-a3c7-b9413da363d7",
                    "transcript_path": str(transcript),
                    "cwd": str(tmp_path),
                    "hook_event_name": "UserPromptSubmit",
                }
            )
            adapter = CodexAdapter(tmp_path / ".codex", lambda _: None)
            self.assertEqual(adapter.resolve_stable_session_id(event), rollout_id)

    def test_unique_recent_rollout_in_same_cwd_resolves_temporary_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            codex_home = root / ".codex"
            rollout_id = "019fab53-93d9-7032-aecf-29b5f9bcc362"
            self.write_rollout(codex_home, rollout_id, root)
            messages: list[str] = []
            adapter = CodexAdapter(codex_home, messages.append)
            event = CodexAdapter.parse_event(
                {"session_id": "019fabce-a3f4-7ab2-8b9f-77f547c23652", "cwd": str(root), "hook_event_name": "Stop"}
            )

            self.assertEqual(adapter.resolve_stable_session_id(event), rollout_id)
            self.assertTrue(any("via recent cwd activity" in message for message in messages))

    def test_two_recent_rollouts_in_same_cwd_are_ambiguous(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            codex_home = root / ".codex"
            self.write_rollout(codex_home, "019fab53-93d9-7032-aecf-29b5f9bcc362", root)
            self.write_rollout(codex_home, "019fab54-93d9-7032-aecf-29b5f9bcc363", root)
            adapter = CodexAdapter(codex_home, lambda _: None)
            event = CodexAdapter.parse_event(
                {"session_id": "019fabce-a3f4-7ab2-8b9f-77f547c23652", "cwd": str(root), "hook_event_name": "Stop"}
            )

            self.assertIsNone(adapter.resolve_stable_session_id(event))

    def test_old_rollout_is_not_used_as_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            codex_home = root / ".codex"
            self.write_rollout(codex_home, "019fab53-93d9-7032-aecf-29b5f9bcc362", root, age_seconds=301)
            adapter = CodexAdapter(codex_home, lambda _: None)
            event = CodexAdapter.parse_event(
                {"session_id": "019fabce-a3f4-7ab2-8b9f-77f547c23652", "cwd": str(root), "hook_event_name": "Stop"}
            )

            self.assertIsNone(adapter.resolve_stable_session_id(event))

    def test_recent_rollout_in_different_cwd_is_not_used(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            codex_home = root / ".codex"
            other = root / "other"
            other.mkdir()
            self.write_rollout(codex_home, "019fab53-93d9-7032-aecf-29b5f9bcc362", other)
            adapter = CodexAdapter(codex_home, lambda _: None)
            event = CodexAdapter.parse_event(
                {"session_id": "019fabce-a3f4-7ab2-8b9f-77f547c23652", "cwd": str(root), "hook_event_name": "Stop"}
            )

            self.assertIsNone(adapter.resolve_stable_session_id(event))

    def test_bridge_origin_is_ignored(self) -> None:
        self.assertTrue(CodexAdapter.bridge_originated({"CC_SESSION_KEY": "feishu:key"}))
        self.assertFalse(CodexAdapter.bridge_originated({}))

    def test_chat_title_uses_latest_session_index_name(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            codex_home = Path(directory)
            session_id = "019f92ba-d95e-7e02-b81b-ddee818481b9"
            (codex_home / "session_index.jsonl").write_text(
                '{"id":"%s","thread_name":"旧标题"}\n' % session_id
                + '{"id":"%s","thread_name":"产品化本地会话编排扩展"}\n' % session_id,
                encoding="utf-8",
            )
            adapter = CodexAdapter(codex_home, lambda _: None)
            self.assertEqual(adapter.chat_title(session_id, "/tmp/repo"), "产品化本地会话编排扩展")
