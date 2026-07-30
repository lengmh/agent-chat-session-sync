from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Callable

from ..models import AgentEvent


ROLLOUT_ID_RE = re.compile(
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$",
    re.IGNORECASE,
)
RECENT_ROLLOUT_SECONDS = 5 * 60


class CodexAdapter:
    agent_type = "codex"

    def __init__(self, codex_home: Path, logger: Callable[[str], None]):
        self.sessions_dir = codex_home / "sessions"
        self.session_index = codex_home / "session_index.jsonl"
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
            turn_id=str(raw.get("turn_id") or raw.get("turnId") or "").strip(),
            agent_type="codex",
            session_title=str(raw.get("session_title") or "").strip(),
        )

    def resolve_stable_session_id(self, event: AgentEvent) -> str | None:
        if event.transcript_path:
            transcript = Path(event.transcript_path)
            match = ROLLOUT_ID_RE.search(transcript.name)
            if match and transcript.is_file():
                return match.group(1).lower()
        if event.session_id and self.sessions_dir.is_dir():
            try:
                next(self.sessions_dir.glob(f"**/*{event.session_id}.jsonl"))
                return event.session_id
            except StopIteration:
                pass
        recent = self._recent_rollout_for_cwd(event.cwd)
        if recent:
            self.logger(
                f"resolved temporary session={event.session_id} to rollout={recent} "
                "via recent cwd activity"
            )
            return recent
        self.logger(f"defer event={event.name} session={event.session_id}: no resumable rollout found")
        return None

    def _recent_rollout_for_cwd(self, cwd: str) -> str | None:
        """Resolve a temporary Desktop hook ID without guessing across sessions.

        Some Codex Desktop hook events omit ``transcript_path`` and expose an
        internal, non-resumable session ID. In that case a rollout that is
        currently being written in the exact same cwd is a safe fallback only
        when it is the sole recent candidate.
        """
        if not cwd or not self.sessions_dir.is_dir():
            return None
        try:
            expected_cwd = Path(cwd).expanduser().resolve()
        except OSError:
            return None

        cutoff = time.time() - RECENT_ROLLOUT_SECONDS
        candidates: list[tuple[float, str]] = []
        for transcript in self.sessions_dir.glob("**/*.jsonl"):
            try:
                modified_at = transcript.stat().st_mtime
                if modified_at < cutoff:
                    continue
                with transcript.open(encoding="utf-8") as handle:
                    first_line = handle.readline()
                entry = json.loads(first_line)
                if entry.get("type") != "session_meta":
                    continue
                payload = entry.get("payload")
                if not isinstance(payload, dict):
                    continue
                metadata_cwd = str(payload.get("cwd", "")).strip()
                if not metadata_cwd or Path(metadata_cwd).expanduser().resolve() != expected_cwd:
                    continue
                session_id = str(payload.get("id") or payload.get("session_id") or "").lower()
                if not ROLLOUT_ID_RE.search(f"{session_id}.jsonl"):
                    continue
                candidates.append((modified_at, session_id))
            except (OSError, json.JSONDecodeError, TypeError, ValueError):
                continue

        if len(candidates) == 1:
            return candidates[0][1]
        if len(candidates) > 1:
            session_ids = ",".join(session_id for _, session_id in sorted(candidates, reverse=True))
            self.logger(f"ambiguous recent rollouts for cwd={cwd}: {session_ids}")
        return None

    @staticmethod
    def bridge_originated(environment: dict[str, str]) -> bool:
        return bool(environment.get("CC_SESSION_KEY"))

    @staticmethod
    def binding_key(session_id: str) -> str:
        # Keep the legacy key stable for existing Codex installations.
        return session_id

    @staticmethod
    def event_text(event: AgentEvent, rollout_id: str = "") -> str | None:
        if event.name == "SessionStart":
            return (
                "已绑定本地 Codex 会话。\n\n"
                f"会话 ID：{rollout_id or event.session_id}\n工作目录：{event.cwd}"
                "\n\n在本群直接发送消息即可继续同一会话，无需 @Bot。"
            )
        if event.name == "UserPromptSubmit":
            return "🧑 本地 Codex\n\n" + event.prompt
        if event.name == "Stop":
            return "🤖 Codex\n\n" + event.assistant_message
        return None

    def session_title(self, session_id: str) -> str | None:
        """Read the latest Codex Desktop title for a rollout.

        session_index.jsonl is append-oriented and may contain more than one
        entry for a thread, so the last matching non-empty name wins.
        """
        title: str | None = None
        try:
            with self.session_index.open(encoding="utf-8") as handle:
                for line in handle:
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if str(entry.get("id", "")).lower() != session_id.lower():
                        continue
                    candidate = str(entry.get("thread_name", "")).strip()
                    if candidate:
                        title = candidate
        except OSError:
            return None
        return title

    def chat_title(self, session_id: str, cwd: str, event: AgentEvent | None = None) -> str:
        native_title = (self.session_title(session_id) or "").strip()
        if native_title:
            title = native_title if native_title.startswith("Codex · ") else f"Codex · {native_title}"
        else:
            title = f"Codex · {Path(cwd).name} · {session_id[:8]}"
        return title[:60]
