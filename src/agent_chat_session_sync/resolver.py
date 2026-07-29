from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path
import re
from typing import Any, Callable, Iterable

from .agents.codex import ROLLOUT_ID_RE
from .models import AgentEvent, ResolutionResult


UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)


@dataclass(frozen=True)
class RolloutCandidate:
    rollout_id: str
    path: Path
    cwd: str
    modified_at: float
    first_prompt: str
    aliases: tuple[str, ...]


class CodexSessionResolver:
    """Resolve temporary Hook identities without treating cwd as an identity protocol."""

    def __init__(self, codex_home: Path, logger: Callable[[str], None]):
        self.codex_home = codex_home
        self.sessions_dir = codex_home / "sessions"
        self.session_index = codex_home / "session_index.jsonl"
        self.global_state_paths = (
            codex_home / "global-state.json",
            codex_home / "global_state.json",
            codex_home / "state.json",
        )
        self.logger = logger

    def resolve(self, event: AgentEvent, received_at: float) -> ResolutionResult:
        exact = self._valid_transcript(event.transcript_path)
        if exact:
            return self._resolved(exact, event, "transcript_path")

        direct = self._find_by_rollout_id(event.session_id)
        if direct:
            return self._resolved(direct, event, "hook_session_id")

        mapped_id = self._mapped_rollout_id(event.session_id)
        if mapped_id:
            mapped = self._find_by_rollout_id(mapped_id)
            if mapped:
                return self._resolved(mapped, event, "codex_state_mapping")

        candidates = self._correlation_candidates(event, received_at)
        if not candidates:
            return ResolutionResult(
                status="pending",
                reason="no rollout candidate yet; waiting for Codex persistence",
            )

        best_score = int(candidates[0][0])
        second_score = int(candidates[1][0]) if len(candidates) > 1 else -1
        evidence = tuple(item[1] for item in candidates[:8])
        if best_score >= 8 and best_score - second_score >= 3:
            path = Path(str(evidence[0]["path"]))
            return self._resolved(path, event, "multi_factor_correlation", evidence)
        return ResolutionResult(
            status="waiting_confirmation",
            candidates=evidence,
            reason=f"ambiguous rollout candidates (best={best_score}, second={second_score})",
        )

    def _resolved(
        self,
        path: Path,
        event: AgentEvent,
        method: str,
        candidates: tuple[dict[str, Any], ...] = (),
    ) -> ResolutionResult:
        match = ROLLOUT_ID_RE.search(path.name)
        if not match:
            return ResolutionResult(status="pending", reason=f"invalid rollout filename: {path}")
        return ResolutionResult(
            status="resolved",
            rollout_id=match.group(1).lower(),
            rollout_path=str(path),
            method=method,
            turn_id=event.turn_id or self.resolve_turn_id(event, path),
            candidates=candidates,
        )

    def _valid_transcript(self, raw_path: str) -> Path | None:
        if not raw_path:
            return None
        path = Path(raw_path).expanduser()
        return path if path.is_file() and ROLLOUT_ID_RE.search(path.name) else None

    def _find_by_rollout_id(self, session_id: str) -> Path | None:
        if not session_id or not UUID_RE.match(session_id) or not self.sessions_dir.is_dir():
            return None
        try:
            return next(self.sessions_dir.glob(f"**/*{session_id.lower()}.jsonl"))
        except StopIteration:
            return None

    def _mapped_rollout_id(self, hook_session_id: str) -> str:
        if not hook_session_id:
            return ""
        for document in self._json_documents((self.session_index, *self.global_state_paths)):
            mapped = self._find_mapping(document, hook_session_id.lower())
            if mapped:
                return mapped
        return ""

    def _json_documents(self, paths: Iterable[Path]) -> Iterable[Any]:
        for path in paths:
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            if path.suffix == ".jsonl":
                for line in text.splitlines():
                    try:
                        yield json.loads(line)
                    except json.JSONDecodeError:
                        continue
            else:
                try:
                    yield json.loads(text)
                except json.JSONDecodeError:
                    continue

    def _find_mapping(self, value: Any, hook_session_id: str) -> str:
        if isinstance(value, dict):
            alias_keys = (
                "session_id",
                "sessionId",
                "hook_session_id",
                "temporary_session_id",
                "source_session_id",
            )
            rollout_keys = ("rollout_id", "rolloutId", "thread_id", "threadId", "id")
            aliases = {str(value.get(key, "")).lower() for key in alias_keys}
            if hook_session_id in aliases:
                for key in rollout_keys:
                    candidate = str(value.get(key, "")).lower()
                    if UUID_RE.match(candidate):
                        return candidate
            for nested in value.values():
                found = self._find_mapping(nested, hook_session_id)
                if found:
                    return found
        elif isinstance(value, list):
            for nested in value:
                found = self._find_mapping(nested, hook_session_id)
                if found:
                    return found
        return ""

    def _correlation_candidates(self, event: AgentEvent, received_at: float) -> list[tuple[int, dict[str, Any]]]:
        if not self.sessions_dir.is_dir():
            return []
        expected_cwd = self._canonical_path(event.cwd)
        scored: list[tuple[int, dict[str, Any]]] = []
        for path in self.sessions_dir.glob("**/*.jsonl"):
            try:
                modified_at = path.stat().st_mtime
            except OSError:
                continue
            # Delayed persistence is expected, but unrelated old rollouts are not useful evidence.
            if abs(modified_at - received_at) > 15 * 60:
                continue
            candidate = self._read_candidate(path)
            if candidate is None:
                continue
            score = 0
            reasons: list[str] = []
            if event.session_id and event.session_id.lower() in candidate.aliases:
                score += 10
                reasons.append("metadata_session_id")
            if expected_cwd and self._canonical_path(candidate.cwd) == expected_cwd:
                score += 4
                reasons.append("cwd")
            delta = abs(candidate.modified_at - received_at)
            if delta <= 10:
                score += 4
                reasons.append("time<=10s")
            elif delta <= 60:
                score += 3
                reasons.append("time<=60s")
            elif delta <= 300:
                score += 1
                reasons.append("time<=5m")
            if event.prompt.strip() and self._same_text(event.prompt, candidate.first_prompt):
                score += 6
                reasons.append("first_prompt")
            if score:
                scored.append(
                    (
                        score,
                        {
                            "rollout_id": candidate.rollout_id,
                            "path": str(candidate.path),
                            "score": score,
                            "reasons": reasons,
                            "modified_at": candidate.modified_at,
                            "cwd": candidate.cwd,
                        },
                    )
                )
        scored.sort(key=lambda item: (item[0], item[1]["modified_at"]), reverse=True)
        return scored

    def _read_candidate(self, path: Path) -> RolloutCandidate | None:
        rollout_match = ROLLOUT_ID_RE.search(path.name)
        if not rollout_match:
            return None
        cwd = ""
        first_prompt = ""
        aliases: set[str] = set()
        try:
            with path.open(encoding="utf-8") as handle:
                for index, line in enumerate(handle):
                    if index > 300:
                        break
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    payload = entry.get("payload", {})
                    if not isinstance(payload, dict):
                        continue
                    if entry.get("type") == "session_meta":
                        cwd = str(payload.get("cwd", ""))
                        for key in ("id", "session_id", "thread_id", "source_session_id"):
                            value = str(payload.get(key, "")).lower()
                            if value:
                                aliases.add(value)
                    if not first_prompt:
                        first_prompt = self._user_text(payload)
        except OSError:
            return None
        return RolloutCandidate(
            rollout_id=rollout_match.group(1).lower(),
            path=path,
            cwd=cwd,
            modified_at=path.stat().st_mtime,
            first_prompt=first_prompt,
            aliases=tuple(aliases),
        )

    @staticmethod
    def _user_text(payload: dict[str, Any]) -> str:
        if payload.get("type") == "user_message":
            return str(payload.get("message", "")).strip()
        if payload.get("type") == "message" and payload.get("role") == "user":
            parts = []
            for item in payload.get("content", []):
                if isinstance(item, dict) and item.get("type") in {"input_text", "text"}:
                    parts.append(str(item.get("text", "")))
            text = "\n".join(parts).strip()
            if text.startswith("<environment_context>") or text.startswith("# AGENTS.md"):
                return ""
            return text
        return ""

    def resolve_turn_id(self, event: AgentEvent, path: Path) -> str:
        matches: list[tuple[float, str]] = []
        try:
            with path.open(encoding="utf-8") as handle:
                for line in handle:
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    payload = entry.get("payload", {})
                    if not isinstance(payload, dict):
                        continue
                    candidate = ""
                    if event.name == "UserPromptSubmit" and self._same_text(event.prompt, self._user_text(payload)):
                        metadata = payload.get("internal_chat_message_metadata_passthrough", {})
                        if isinstance(metadata, dict):
                            candidate = str(metadata.get("turn_id", ""))
                    elif event.name == "Stop" and payload.get("type") == "task_complete":
                        if self._same_text(event.assistant_message, str(payload.get("last_agent_message", ""))):
                            candidate = str(payload.get("turn_id", ""))
                    if candidate:
                        matches.append((self._timestamp(entry.get("timestamp")), candidate))
        except OSError:
            return ""
        return max(matches, default=(0.0, ""))[1]

    @staticmethod
    def _timestamp(value: Any) -> float:
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
        except ValueError:
            return 0.0

    @staticmethod
    def _canonical_path(value: str) -> str:
        if not value:
            return ""
        try:
            return str(Path(value).expanduser().resolve())
        except OSError:
            return ""

    @staticmethod
    def _same_text(left: str, right: str) -> bool:
        return " ".join(left.split()) == " ".join(right.split()) and bool(left.strip())
