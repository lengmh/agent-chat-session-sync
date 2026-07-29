from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Protocol

from ..models import AgentEvent, ResolutionResult


class SessionResolver(Protocol):
    def resolve(self, event: AgentEvent, received_at: float) -> ResolutionResult: ...

    def resolve_turn_id(self, event: AgentEvent, path: Path) -> str: ...


class AgentAdapter(Protocol):
    agent_type: str

    def parse_event(self, raw: dict[str, Any]) -> AgentEvent: ...

    def bridge_originated(self, environment: dict[str, str]) -> bool: ...

    def event_text(self, event: AgentEvent, session_id: str = "") -> str | None: ...

    def chat_title(self, session_id: str, cwd: str, event: AgentEvent | None = None) -> str: ...

    def binding_key(self, session_id: str) -> str: ...


AdapterFactory = Callable[[Callable[[str], None]], tuple[AgentAdapter, SessionResolver]]
