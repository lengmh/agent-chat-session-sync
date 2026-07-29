from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class AgentEvent:
    name: str
    session_id: str
    cwd: str
    transcript_path: str = ""
    prompt: str = ""
    assistant_message: str = ""
    turn_id: str = ""


@dataclass(frozen=True)
class ResolutionResult:
    status: str
    rollout_id: str = ""
    rollout_path: str = ""
    method: str = ""
    turn_id: str = ""
    candidates: tuple[dict[str, Any], ...] = ()
    reason: str = ""


@dataclass(frozen=True)
class Project:
    name: str
    work_dir: str
    mode: str
    base_dir: str
    agent_type: str
    platform_type: str
    platform_options: dict[str, Any]


@dataclass(frozen=True)
class Binding:
    chat_id: str
    session_key: str
    project: str
    cwd: str
    generation: int
    created_at: str
    title: str = ""

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Binding":
        return cls(
            chat_id=str(value.get("chat_id", "")),
            session_key=str(value.get("session_key", "")),
            project=str(value.get("project", "")),
            cwd=str(value.get("cwd", "")),
            generation=int(value.get("generation", 1)),
            created_at=str(value.get("created_at", "")),
            title=str(value.get("title", "")),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
