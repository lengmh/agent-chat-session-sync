from __future__ import annotations

import signal
import time
from pathlib import Path
import json
import os
from typing import Callable

from .agents.codex import CodexAdapter
from .agents.claude_code import ClaudeCodeAdapter
from .bridges.cc_connect import CCConnectBridge
from .config import Settings, load_cc_connect_config, matching_project
from .coordinator import PlatformFactory, SessionCoordinator
from .endpoints import LocalEndpoint
from .models import ResolutionResult
from .permissions import SecurityCheck, local_endpoint_security_checks
from .platforms.feishu import FeishuPlatform
from .queue import EventDatabase, SQLiteBindingStore, stable_event_id
from .resolver import CodexSessionResolver
from .claude_resolver import ClaudeSessionResolver


class EventWorker:
    def __init__(
        self,
        settings: Settings,
        logger: Callable[[str], None],
        platform_factory: PlatformFactory | None = None,
        endpoint_security_checker: Callable[
            [str, LocalEndpoint],
            list[SecurityCheck],
        ] = local_endpoint_security_checks,
    ):
        self.settings = settings
        self.logger = logger
        self.database = EventDatabase(settings.database_path)
        imported = self.database.import_legacy_bindings(settings.state_path)
        if imported:
            self.logger(f"imported legacy bindings count={imported}")
        self.config = load_cc_connect_config(settings.cc_config)
        self.agent = CodexAdapter(settings.codex_home, logger)
        self.resolver = CodexSessionResolver(settings.codex_home, logger)
        self.claude_agent = ClaudeCodeAdapter(settings.claude_home, logger)
        self.claude_resolver = ClaudeSessionResolver(settings.claude_home, logger)
        self.platform_factory = platform_factory or (
            lambda project: FeishuPlatform.from_options(project.platform_options)
        )
        self.endpoint_security_checker = endpoint_security_checker
        self.coordinator = SessionCoordinator(
            self.agent,
            CCConnectBridge(settings.local_endpoint),
            SQLiteBindingStore(self.database),
            self.config,
            logger,
            platform_factory=self.platform_factory,
        )
        self.claude_coordinator = SessionCoordinator(
            self.claude_agent,
            CCConnectBridge(settings.local_endpoint),
            SQLiteBindingStore(self.database),
            self.config,
            logger,
            platform_factory=self.platform_factory,
        )
        self._last_bridge_instance_id: str | None = None

    def components(self, agent_type: str):
        if agent_type == "claudecode":
            return self.claude_agent, self.claude_resolver, self.claude_coordinator
        return self.agent, self.resolver, self.coordinator

    def replay_bindings(self) -> tuple[int, int]:
        """Reattach durable bindings and rebuild dynamic Feishu routes.

        A replay is deliberately best-effort: an unavailable cc-connect daemon
        must not prevent durable Hook receipts from being processed and retried.
        """
        replayed = 0
        failed = 0
        for binding_key, binding in self.database.list_bindings():
            agent_type = "claudecode" if binding_key.startswith("claudecode:") else "codex"
            session_id = binding_key.split(":", 1)[1] if agent_type == "claudecode" else binding_key
            _agent, _resolver, coordinator = self.components(agent_type)
            try:
                coordinator.bridge.attach_agent_session(
                    binding.project,
                    binding.session_key,
                    session_id,
                    binding.title or f"{agent_type} · {session_id[:8]}",
                    binding.cwd,
                )
                replayed += 1
            except Exception as exc:
                failed += 1
                self.logger(
                    f"binding replay deferred key={binding_key} project={binding.project} error={exc}"
                )
        if replayed or failed:
            self.logger(f"binding replay complete replayed={replayed} failed={failed}")
        return replayed, failed

    def _maybe_replay_bindings(self) -> bool:
        try:
            security_checks = self.endpoint_security_checker(
                "cc-connect endpoint",
                self.settings.local_endpoint,
            )
        except Exception as exc:
            self.logger(f"cc-connect endpoint security audit failed error={exc}")
            return False
        failed_checks = [check for check in security_checks if not check.okay]
        if failed_checks:
            detail = "; ".join(
                f"{check.name}: {check.detail}" for check in failed_checks
            )
            self.logger(f"cc-connect endpoint rejected {detail}")
            return False
        try:
            info = self.coordinator.bridge.inspect()
        except Exception as exc:
            self.logger(f"cc-connect endpoint unavailable error={exc}")
            return False
        required = {
            "attach_agent_session",
            "binding_routing",
            "local_endpoint_v2",
        }
        if not required.issubset(info.capabilities):
            return False
        if info.instance_id == self._last_bridge_instance_id:
            return True
        _replayed, failed = self.replay_bindings()
        if failed:
            return False
        self._last_bridge_instance_id = info.instance_id
        return True

    def import_emergency_spool(self) -> int:
        spool = self.settings.data_dir / "emergency-inbox.jsonl"
        if not spool.exists():
            return 0
        processing = spool.with_name(f"{spool.name}.processing.{os.getpid()}")
        try:
            os.replace(spool, processing)
        except FileNotFoundError:
            return 0
        imported = 0
        try:
            with processing.open(encoding="utf-8") as handle:
                for line in handle:
                    try:
                        item = json.loads(line)
                        self.database.enqueue(
                            item["raw"],
                            bool(item.get("bridge_originated")),
                            float(item.get("received_at") or time.time()),
                            str(item.get("agent_type") or "codex"),
                        )
                        imported += 1
                    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                        self.logger(f"invalid emergency spool record file={processing} error={exc}")
        finally:
            processing.unlink(missing_ok=True)
        if imported:
            self.logger(f"imported emergency spool count={imported}")
        return imported

    @staticmethod
    def retry_delay(attempt: int) -> float:
        return float(min(300, max(1, 2 ** min(max(attempt - 1, 0), 8))))

    def run_once(self) -> bool:
        bridge_ready = self._maybe_replay_bindings()
        self.import_emergency_spool()
        queued = self.database.claim_due()
        if queued is None:
            return False
        if queued.bridge_originated:
            self.database.transition(queued.id, "delivered", last_error="bridge-originated event ignored")
            self.logger(f"event delivered inbox={queued.id} ignored=bridge_origin")
            return True

        agent, resolver, coordinator = self.components(queued.agent_type)
        event = agent.parse_event(queued.raw)
        if not event.cwd:
            self.database.retry(
                queued.id,
                "resolving_session",
                "hook event does not contain cwd",
                self.retry_delay(queued.attempts),
            )
            return True

        if queued.resolution_method == "manual_confirmation" and queued.rollout_id and queued.rollout_path:
            resolution = ResolutionResult(
                status="resolved",
                rollout_id=queued.rollout_id,
                rollout_path=queued.rollout_path,
                method=queued.resolution_method,
                turn_id=event.turn_id or resolver.resolve_turn_id(event, Path(queued.rollout_path)),
            )
        else:
            resolution = resolver.resolve(event, queued.received_at)
        if resolution.status != "resolved":
            state = "waiting_confirmation" if resolution.status == "waiting_confirmation" else "resolving_session"
            self.database.retry(
                queued.id,
                state,
                resolution.reason,
                self.retry_delay(queued.attempts),
                resolution.candidates,
            )
            self.logger(
                f"event deferred inbox={queued.id} state={state} attempts={queued.attempts} reason={resolution.reason}"
            )
            return True

        self.database.transition(
            queued.id,
            "resolved",
            rollout_id=resolution.rollout_id,
            rollout_path=resolution.rollout_path,
            resolution_method=resolution.method,
            candidates=resolution.candidates,
            last_error="",
        )
        turn_id = resolution.turn_id or event.turn_id
        if event.name in {"UserPromptSubmit", "Stop"} and not turn_id:
            if queued.attempts < 10:
                self.database.retry(
                    queued.id,
                    "resolving_session",
                    "rollout resolved but turn_id is not persisted yet",
                    self.retry_delay(queued.attempts),
                )
                return True
            turn_id = f"receipt:{queued.receipt_id[:24]}"
            self.logger(f"event turn fallback inbox={queued.id} discriminator={turn_id}")
        project = matching_project(self.config, event.cwd, queued.agent_type)
        if project is None:
            self.database.retry(
                queued.id,
                "resolved",
                "cwd is not covered by a Feishu cc-connect project",
                self.retry_delay(queued.attempts),
            )
            return True

        if not bridge_ready:
            delay = self.retry_delay(queued.attempts)
            error = "cc-connect local endpoint is unavailable or incompatible"
            self.database.retry(queued.id, "binding_chat", error, delay)
            self.logger(
                f"event deferred inbox={queued.id} state=binding_chat attempts={queued.attempts} "
                f"delay={delay} error={error}"
            )
            return True

        try:
            platform = self.platform_factory(project)
            self.database.transition(queued.id, "binding_chat")
            binding = coordinator.ensure_binding(resolution.rollout_id, event, project, platform)
            payload = agent.event_text(event, resolution.rollout_id) or ""
            eid = stable_event_id(
                resolution.rollout_id,
                event.name,
                turn_id,
                payload,
                queued.agent_type,
            )
            outbox = self.database.ensure_outbox(eid, queued.id, resolution.rollout_id, event.name, payload)
            self.database.transition(queued.id, "sending", stable_event_id=eid)
            if outbox.status == "delivered":
                self.database.transition(queued.id, "delivered", last_error="")
                self.logger(f"event deduplicated inbox={queued.id} event_id={eid}")
                return True
            if not payload:
                self.database.mark_outbox_delivered(eid, "no-message")
                self.database.transition(queued.id, "delivered", last_error="")
                return True
            self.database.mark_outbox_sending(eid)
            binding, message_id = coordinator.deliver(
                resolution.rollout_id,
                event,
                project,
                platform,
                binding,
                payload,
                eid,
            )
            self.database.mark_outbox_delivered(eid, message_id)
            self.database.transition(queued.id, "delivered", last_error="")
            self.logger(
                f"event delivered inbox={queued.id} event_id={eid} rollout={resolution.rollout_id} "
                f"method={resolution.method} platform_message_id={message_id or '<not-returned>'}"
            )
        except Exception as exc:
            delay = self.retry_delay(queued.attempts)
            current = self.database.get_event(queued.id)
            eid = current.stable_event_id if current else ""
            if eid:
                self.database.mark_outbox_retry(eid, str(exc), delay)
            self.database.retry(queued.id, "sending", str(exc), delay)
            self.logger(f"event retry inbox={queued.id} attempts={queued.attempts} delay={delay} error={exc}")
        return True

    def run_forever(self, poll_interval: float = 1.0) -> None:
        stopping = False

        def stop(_signum: int, _frame: object) -> None:
            nonlocal stopping
            stopping = True

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        self.logger(f"worker started database={self.settings.database_path}")
        while not stopping:
            processed = self.run_once()
            if not processed:
                time.sleep(poll_interval)
        self.logger("worker stopped")
