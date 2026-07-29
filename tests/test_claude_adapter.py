from pathlib import Path
import json
import tempfile
import unittest

from agent_chat_session_sync.agents.claude_code import ClaudeCodeAdapter


class ClaudeCodeAdapterTests(unittest.TestCase):
    def test_hook_fields_and_bridge_origin_are_native(self) -> None:
        raw = {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "A0B1C2D3-1111-2222-3333-444455556666",
            "prompt_id": "prompt-1",
            "cwd": "/work",
            "prompt": "hello",
        }
        event = ClaudeCodeAdapter.parse_event(raw)
        self.assertEqual(event.session_id, raw["session_id"].lower())
        self.assertEqual(event.turn_id, "prompt-1")
        self.assertEqual(event.agent_type, "claudecode")
        self.assertEqual(ClaudeCodeAdapter.binding_key(event.session_id), f"claudecode:{event.session_id}")
        self.assertTrue(ClaudeCodeAdapter.bridge_originated({"CC_SESSION_KEY": "feishu:key"}))

    def test_title_uses_first_transcript_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw) / ".claude"
            session_id = "a0b1c2d3-1111-2222-3333-444455556666"
            transcript = home / "projects/project" / f"{session_id}.jsonl"
            transcript.parent.mkdir(parents=True)
            transcript.write_text(
                json.dumps({"type": "user", "message": {"content": "设计可靠的同步服务"}}) + "\n",
                encoding="utf-8",
            )
            title = ClaudeCodeAdapter(home, lambda _: None).chat_title(session_id, "/work")
            self.assertEqual(title, "Claude · 设计可靠的同步服务")


if __name__ == "__main__":
    unittest.main()
