from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any, Callable

from .agents.claude_code import ClaudeCodeAdapter, UUID_RE
from .models import AgentEvent, ResolutionResult


TRANSCRIPT_RE = re.compile(
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$",
    re.IGNORECASE,
)


class ClaudeSessionResolver:
    """Resolve Claude Code sessions using the native stable session UUID."""

    def __init__(self, claude_home: Path, logger: Callable[[str], None]):
        self.projects_dir = claude_home / "projects"
        self.history_path = claude_home / "history.jsonl"
        self.logger = logger

    def resolve(self, event: AgentEvent, received_at: float) -> ResolutionResult:
        exact = self._valid_transcript(event.transcript_path)
        if exact is not None:
            return self._resolved(exact, event, "transcript_path")

        direct = self._find_by_session_id(event.session_id)
        if direct is not None:
            return self._resolved(direct, event, "hook_session_id")

        history_id = self._history_session(event)
        if history_id:
            history_path = self._find_by_session_id(history_id)
            if history_path is not None:
                return self._resolved(history_path, event, "claude_history")

        candidates = self._correlation_candidates(event, received_at)
        if not candidates:
            return ResolutionResult(status="pending", reason="no Claude transcript yet; waiting for persistence")
        best_score = int(candidates[0]["score"])
        second_score = int(candidates[1]["score"]) if len(candidates) > 1 else -1
        evidence = tuple(candidates[:8])
        if best_score >= 8 and best_score - second_score >= 3:
            return self._resolved(Path(str(evidence[0]["path"])), event, "multi_factor_correlation", evidence)
        return ResolutionResult(
            status="waiting_confirmation",
            candidates=evidence,
            reason=f"ambiguous Claude session candidates (best={best_score}, second={second_score})",
        )

    def _resolved(
        self,
        path: Path,
        event: AgentEvent,
        method: str,
        candidates: tuple[dict[str, Any], ...] = (),
    ) -> ResolutionResult:
        match = TRANSCRIPT_RE.search(path.name)
        if not match:
            return ResolutionResult(status="pending", reason=f"invalid Claude transcript filename: {path}")
        return ResolutionResult(
            status="resolved",
            rollout_id=match.group(1).lower(),
            rollout_path=str(path),
            method=method,
            turn_id=event.turn_id or self.resolve_turn_id(event, path),
            candidates=candidates,
        )

    @staticmethod
    def _valid_transcript(raw_path: str) -> Path | None:
        if not raw_path:
            return None
        path = Path(raw_path).expanduser()
        return path if path.is_file() and TRANSCRIPT_RE.search(path.name) else None

    def _find_by_session_id(self, session_id: str) -> Path | None:
        if not UUID_RE.match(session_id) or not self.projects_dir.is_dir():
            return None
        try:
            return next(self.projects_dir.glob(f"**/{session_id.lower()}.jsonl"))
        except StopIteration:
            return None

    def _history_session(self, event: AgentEvent) -> str:
        if not event.cwd or not event.prompt:
            return ""
        found = ""
        try:
            with self.history_path.open(encoding="utf-8") as handle:
                for line in handle:
                    try:
                        item = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if str(item.get("project", "")) != event.cwd:
                        continue
                    if self._same_text(str(item.get("display", "")), event.prompt):
                        candidate = str(item.get("sessionId", "")).lower()
                        if UUID_RE.match(candidate):
                            found = candidate
        except OSError:
            pass
        return found

    def _correlation_candidates(self, event: AgentEvent, received_at: float) -> list[dict[str, Any]]:
        if not self.projects_dir.is_dir():
            return []
        expected_cwd = self._canonical(event.cwd)
        scored: list[dict[str, Any]] = []
        for path in self.projects_dir.glob("**/*.jsonl"):
            if path.parent.name == "subagents":
                continue
            match = TRANSCRIPT_RE.search(path.name)
            if not match:
                continue
            try:
                modified = path.stat().st_mtime
            except OSError:
                continue
            if abs(modified - received_at) > 15 * 60:
                continue
            metadata = self._transcript_metadata(path)
            score = 0
            reasons: list[str] = []
            if event.session_id and metadata["session_id"] == event.session_id:
                score += 10
                reasons.append("metadata_session_id")
            if expected_cwd and self._canonical(metadata["cwd"]) == expected_cwd:
                score += 4
                reasons.append("cwd")
            delta = abs(modified - received_at)
            if delta <= 10:
                score += 4
                reasons.append("time<=10s")
            elif delta <= 60:
                score += 3
                reasons.append("time<=60s")
            elif delta <= 300:
                score += 1
                reasons.append("time<=5m")
            if event.prompt and self._same_text(event.prompt, metadata["first_prompt"]):
                score += 6
                reasons.append("first_prompt")
            if score:
                scored.append(
                    {
                        "rollout_id": match.group(1).lower(),
                        "path": str(path),
                        "score": score,
                        "reasons": reasons,
                        "modified_at": modified,
                        "cwd": metadata["cwd"],
                    }
                )
        scored.sort(key=lambda item: (int(item["score"]), float(item["modified_at"])), reverse=True)
        return scored

    @staticmethod
    def _transcript_metadata(path: Path) -> dict[str, str]:
        result = {"session_id": "", "cwd": "", "first_prompt": ""}
        try:
            with path.open(encoding="utf-8") as handle:
                for index, line in enumerate(handle):
                    if index > 300:
                        break
                    try:
                        item = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    result["session_id"] = result["session_id"] or str(item.get("sessionId", "")).lower()
                    result["cwd"] = result["cwd"] or str(item.get("cwd", ""))
                    if not result["first_prompt"] and item.get("type") == "user" and not item.get("isMeta"):
                        message = item.get("message", {})
                        if isinstance(message, dict):
                            result["first_prompt"] = ClaudeCodeAdapter.message_text(message.get("content"))
                    if all(result.values()):
                        break
        except OSError:
            pass
        return result

    def resolve_turn_id(self, event: AgentEvent, path: Path) -> str:
        if event.turn_id:
            return event.turn_id
        matches: list[str] = []
        try:
            with path.open(encoding="utf-8") as handle:
                for line in handle:
                    try:
                        item = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    message = item.get("message", {})
                    if not isinstance(message, dict):
                        continue
                    text = ClaudeCodeAdapter.message_text(message.get("content"))
                    if event.name == "UserPromptSubmit" and item.get("type") == "user" and self._same_text(text, event.prompt):
                        matches.append(str(item.get("promptId") or item.get("uuid") or ""))
                    elif event.name == "Stop" and item.get("type") == "assistant" and self._same_text(text, event.assistant_message):
                        matches.append(str(item.get("promptId") or item.get("uuid") or ""))
        except OSError:
            return ""
        return next((value for value in reversed(matches) if value), "")

    @staticmethod
    def _canonical(value: str) -> str:
        if not value:
            return ""
        try:
            return str(Path(value).expanduser().resolve())
        except OSError:
            return ""

    @staticmethod
    def _same_text(left: str, right: str) -> bool:
        return " ".join(left.split()) == " ".join(right.split()) and bool(left.strip())
