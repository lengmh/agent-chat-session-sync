from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any, Callable

from ..models import AgentEvent


UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)


class ClaudeCodeAdapter:
    agent_type = "claudecode"

    def __init__(self, claude_home: Path, logger: Callable[[str], None]):
        self.claude_home = claude_home
        self.projects_dir = claude_home / "projects"
        self.logger = logger

    @staticmethod
    def parse_event(raw: dict[str, Any]) -> AgentEvent:
        return AgentEvent(
            name=str(raw.get("hook_event_name", "")),
            session_id=str(raw.get("session_id", "")).strip().lower(),
            cwd=str(raw.get("cwd", "")).strip(),
            transcript_path=str(raw.get("transcript_path") or "").strip(),
            prompt=str(raw.get("prompt", "")),
            assistant_message=str(raw.get("last_assistant_message", "")),
            turn_id=str(raw.get("prompt_id") or raw.get("turn_id") or "").strip(),
            agent_type="claudecode",
            session_title=str(raw.get("session_title") or "").strip(),
        )

    @staticmethod
    def bridge_originated(environment: dict[str, str]) -> bool:
        return bool(environment.get("CC_SESSION_KEY") or environment.get("ACSS_BRIDGE_ORIGIN"))

    @staticmethod
    def binding_key(session_id: str) -> str:
        return f"claudecode:{session_id}"

    @staticmethod
    def event_text(event: AgentEvent, session_id: str = "") -> str | None:
        if event.name == "SessionStart":
            return (
                "已绑定本地 Claude Code 会话。\n\n"
                f"会话 ID：{session_id or event.session_id}\n工作目录：{event.cwd}"
                "\n\n在本群直接发送消息即可继续同一会话，无需 @Bot。"
            )
        if event.name == "UserPromptSubmit":
            return "🧑 本地 Claude Code\n\n" + event.prompt
        if event.name == "Stop":
            return "🤖 Claude Code\n\n" + event.assistant_message
        return None

    def chat_title(self, session_id: str, cwd: str, event: AgentEvent | None = None) -> str:
        explicit = event.session_title.strip() if event else ""
        prompt = self._first_prompt(session_id)
        if explicit:
            title = explicit if explicit.startswith("Claude · ") else f"Claude · {explicit}"
        elif prompt:
            single_line = " ".join(prompt.split())
            title = f"Claude · {single_line}"
        else:
            title = f"Claude · {Path(cwd).name} · {session_id[:8]}"
        return title[:60]

    def _first_prompt(self, session_id: str) -> str:
        transcript = self._find_transcript(session_id)
        if transcript is None:
            return ""
        try:
            with transcript.open(encoding="utf-8") as handle:
                for line in handle:
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if entry.get("type") != "user" or entry.get("isMeta"):
                        continue
                    text = self.message_text(entry.get("message", {}).get("content"))
                    if text and not text.startswith("<"):
                        return text
        except OSError:
            pass
        return ""

    def _find_transcript(self, session_id: str) -> Path | None:
        if not UUID_RE.match(session_id) or not self.projects_dir.is_dir():
            return None
        try:
            return next(self.projects_dir.glob(f"**/{session_id}.jsonl"))
        except StopIteration:
            return None

    @staticmethod
    def message_text(content: Any) -> str:
        if isinstance(content, str):
            return content.strip()
        if not isinstance(content, list):
            return ""
        parts = [
            str(item.get("text", ""))
            for item in content
            if isinstance(item, dict) and item.get("type") == "text"
        ]
        return "\n".join(part for part in parts if part).strip()
