from __future__ import annotations

from datetime import datetime
from typing import Callable

from .agents.base import AgentAdapter
from .bridges.cc_connect import CCConnectBridge
from .config import matching_project
from .errors import PlatformAPIError
from .models import AgentEvent, Binding, Project
from .platforms.feishu import FeishuPlatform
from .store import BindingStore


PlatformFactory = Callable[[Project], FeishuPlatform]


class SessionCoordinator:
    def __init__(
        self,
        agent: AgentAdapter,
        bridge: CCConnectBridge,
        store: BindingStore,
        config: dict,
        logger: Callable[[str], None],
        platform_factory: PlatformFactory | None = None,
    ):
        self.agent = agent
        self.bridge = bridge
        self.store = store
        self.config = config
        self.logger = logger
        self.platform_factory = platform_factory or (lambda project: FeishuPlatform.from_options(project.platform_options))

    def handle(self, event: AgentEvent) -> Binding | None:
        session_id = self.agent.resolve_stable_session_id(event)
        if not session_id or not event.cwd:
            return None
        project = matching_project(self.config, event.cwd, event.agent_type)
        if project is None:
            self.logger(f"skip session={session_id}: cwd is not covered by a Feishu cc-connect project")
            return None
        platform = self.platform_factory(project)
        binding = self._ensure_binding(session_id, event, project, platform)
        binding = self._sync_title(session_id, event, platform, binding)
        text = self.agent.event_text(event)
        if text:
            binding, _ = self._send_with_recovery(session_id, event, project, platform, binding, text)
        return binding

    def _ensure_binding(self, session_id: str, event: AgentEvent, project: Project, platform: FeishuPlatform) -> Binding:
        binding_key = self.agent.binding_key(session_id)
        existing = self.store.get(binding_key)
        if existing and platform.validate_chat(existing.chat_id):
            return existing
        generation = (existing.generation + 1) if existing else 1
        if existing:
            self._discard(session_id, existing, "chat unavailable during validation")
        return self._create(session_id, event, project, platform, generation)

    def _create(
        self, session_id: str, event: AgentEvent, project: Project, platform: FeishuPlatform, generation: int
    ) -> Binding:
        title = self.agent.chat_title(session_id, event.cwd, event)
        chat_id = platform.create_session_chat(session_id, event.cwd, generation, title)
        session_key = platform.session_key(chat_id)
        self.bridge.attach_agent_session(project.name, session_key, session_id, title, event.cwd)
        binding = Binding(
            chat_id=chat_id,
            session_key=session_key,
            project=project.name,
            cwd=event.cwd,
            generation=generation,
            created_at=datetime.now().astimezone().isoformat(timespec="seconds"),
            title=title,
        )
        self.store.bind(self.agent.binding_key(session_id), binding)
        self.logger(f"created session={session_id} chat={chat_id} project={project.name} generation={generation}")
        return binding

    def ensure_binding(
        self, session_id: str, event: AgentEvent, project: Project, platform: FeishuPlatform
    ) -> Binding:
        binding = self._ensure_binding(session_id, event, project, platform)
        return self._sync_title(session_id, event, platform, binding)

    def deliver(
        self,
        session_id: str,
        event: AgentEvent,
        project: Project,
        platform: FeishuPlatform,
        binding: Binding,
        text: str,
        idempotency_key: str,
    ) -> tuple[Binding, str]:
        return self._send_with_recovery(
            session_id, event, project, platform, binding, text, idempotency_key
        )

    def _sync_title(
        self, session_id: str, event: AgentEvent, platform: FeishuPlatform, binding: Binding
    ) -> Binding:
        title = self.agent.chat_title(session_id, event.cwd, event)
        if binding.title == title:
            return binding
        platform.rename_chat(binding.chat_id, title)
        updated = Binding(
            chat_id=binding.chat_id,
            session_key=binding.session_key,
            project=binding.project,
            cwd=binding.cwd,
            generation=binding.generation,
            created_at=binding.created_at,
            title=title,
        )
        self.store.bind(self.agent.binding_key(session_id), updated)
        self.logger(f"renamed session={session_id} chat={binding.chat_id} title={title!r}")
        return updated

    def _discard(self, session_id: str, binding: Binding, reason: str) -> None:
        if self.store.invalidate(self.agent.binding_key(session_id), binding.chat_id):
            self.logger(f"removed stale mapping session={session_id} chat={binding.chat_id} reason={reason}")

    def _send_with_recovery(
        self,
        session_id: str,
        event: AgentEvent,
        project: Project,
        platform: FeishuPlatform,
        binding: Binding,
        text: str,
        idempotency_key: str = "",
    ) -> tuple[Binding, str]:
        try:
            message_id = platform.send_message(binding.chat_id, text, idempotency_key)
            return binding, message_id
        except PlatformAPIError:
            if platform.validate_chat(binding.chat_id):
                raise
        self._discard(session_id, binding, "send failed and chat unavailable")
        replacement = self._create(session_id, event, project, platform, binding.generation + 1)
        message_id = platform.send_message(replacement.chat_id, text, idempotency_key)
        self.logger(f"resent event={event.name} session={session_id} replacement_chat={replacement.chat_id}")
        return replacement, message_id
