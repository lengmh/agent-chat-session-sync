from pathlib import Path
import json
import os
import tempfile
import time
import unittest

from agent_chat_session_sync.claude_resolver import ClaudeSessionResolver
from agent_chat_session_sync.models import AgentEvent


def write_claude_transcript(home: Path, session_id: str, cwd: Path, prompt: str, modified: float) -> Path:
    path = home / "projects" / "encoded-project" / f"{session_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "type": "user",
                "sessionId": session_id,
                "cwd": str(cwd),
                "promptId": "prompt-from-transcript",
                "message": {"content": prompt},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    os.utime(path, (modified, modified))
    return path


class ClaudeSessionResolverTests(unittest.TestCase):
    def test_transcript_path_precedes_hook_session_id(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            home = root / ".claude"
            now = time.time()
            first = write_claude_transcript(home, "a0b1c2d3-1111-2222-3333-444455556666", root, "one", now)
            write_claude_transcript(home, "b0b1c2d3-1111-2222-3333-444455556666", root, "two", now)
            event = AgentEvent(
                "UserPromptSubmit",
                "b0b1c2d3-1111-2222-3333-444455556666",
                str(root),
                str(first),
                prompt="one",
                agent_type="claudecode",
            )
            result = ClaudeSessionResolver(home, lambda _: None).resolve(event, now)
            self.assertEqual(result.rollout_id, "a0b1c2d3-1111-2222-3333-444455556666")
            self.assertEqual(result.method, "transcript_path")

    def test_delayed_transcript_and_history_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            home = root / ".claude"
            resolver = ClaudeSessionResolver(home, lambda _: None)
            session_id = "a0b1c2d3-1111-2222-3333-444455556666"
            event = AgentEvent("UserPromptSubmit", session_id, str(root), prompt="delayed", agent_type="claudecode")
            now = time.time()
            self.assertEqual(resolver.resolve(event, now).status, "pending")
            transcript = write_claude_transcript(home, session_id, root, "delayed", now)
            (home / "history.jsonl").write_text(
                json.dumps({"project": str(root), "display": "delayed", "sessionId": session_id}) + "\n",
                encoding="utf-8",
            )
            result = resolver.resolve(event, now)
            self.assertEqual(result.rollout_path, str(transcript))
            self.assertIn(result.method, {"hook_session_id", "claude_history"})

    def test_same_cwd_two_sessions_waits_for_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            home = root / ".claude"
            now = time.time()
            write_claude_transcript(home, "a0b1c2d3-1111-2222-3333-444455556666", root, "one", now)
            write_claude_transcript(home, "b0b1c2d3-1111-2222-3333-444455556666", root, "two", now)
            event = AgentEvent("SessionStart", "not-yet-persisted", str(root), agent_type="claudecode")
            result = ClaudeSessionResolver(home, lambda _: None).resolve(event, now)
            self.assertEqual(result.status, "waiting_confirmation")
            self.assertEqual(len(result.candidates), 2)


if __name__ == "__main__":
    unittest.main()
