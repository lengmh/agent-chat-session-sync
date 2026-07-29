from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import shutil
import subprocess
import time
import uuid
from typing import Callable

from .config import Settings, load_cc_connect_config, matching_project
from .platforms.feishu import FeishuPlatform
from .queue import EventDatabase


@dataclass(frozen=True)
class AcceptanceResult:
    rollout_id: str
    chat_id: str
    token: str
    reply_token: str
    resources_cleaned: bool


class LiveAcceptance:
    """Real Codex → Hook → worker → Feishu → cc-connect acceptance harness."""

    def __init__(self, settings: Settings, logger: Callable[[str], None]):
        self.settings = settings
        self.logger = logger
        self.database = EventDatabase(settings.database_path)

    def run(self, timeout: float = 300, keep_resources: bool = False, skip_reply: bool = False) -> AcceptanceResult:
        token = f"ACSS-E2E-{uuid.uuid4().hex[:10]}"
        reply_token = f"{token}-REPLY"
        workspace = self.settings.data_dir / "acceptance" / token.lower()
        workspace.mkdir(parents=True, exist_ok=False)
        (workspace / "README.md").write_text("agent-chat-session-sync live acceptance workspace\n", encoding="utf-8")
        subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
        prompt = f"Reply with exactly this token and no other text: {token}"
        command = [
            "codex",
            "exec",
            "--json",
            "--sandbox",
            "read-only",
            "--dangerously-bypass-hook-trust",
            "-C",
            str(workspace),
            prompt,
        ]
        self.logger(f"acceptance codex start token={token} workspace={workspace}")
        result = subprocess.run(command, text=True, capture_output=True, timeout=timeout)
        if result.returncode != 0:
            raise RuntimeError(f"Codex acceptance session failed: {result.stderr[-2000:]}")
        rollout_id = self._thread_id(result.stdout)
        if not rollout_id:
            raise RuntimeError("Codex acceptance output did not contain thread_id")

        deadline = time.time() + timeout
        required = {"UserPromptSubmit", "Stop"}
        while time.time() < deadline:
            events = self.database.events_for_rollout(rollout_id)
            delivered = {
                str(event.raw.get("hook_event_name", ""))
                for event in events
                if event.state == "delivered"
            }
            if required <= delivered:
                break
            time.sleep(1)
        else:
            states = [(event.id, event.state, event.last_error) for event in self.database.events_for_rollout(rollout_id)]
            raise TimeoutError(f"Hook events were not delivered for rollout {rollout_id}: {states}")

        binding = self.database.get_binding(rollout_id)
        if binding is None:
            raise RuntimeError(f"no chat binding for acceptance rollout {rollout_id}")
        outbox = self.database.outbox_for_rollout(rollout_id)
        messages = [item for item in outbox if item.status == "delivered" and item.platform_message_id not in {"", "no-message"}]
        if len(messages) < 2:
            raise RuntimeError(f"expected prompt and assistant outbox messages, got {len(messages)}")

        config = load_cc_connect_config(self.settings.cc_config)
        project = matching_project(config, str(workspace))
        if project is None:
            raise RuntimeError("acceptance workspace is not covered by cc-connect config")
        platform = FeishuPlatform.from_options(project.platform_options)
        for item in messages:
            for message_id in item.platform_message_id.split(","):
                if message_id and not platform.get_message(message_id):
                    raise RuntimeError(f"platform message cannot be read back: {message_id}")

        if not skip_reply:
            print(f"请在飞书测试群发送：{reply_token}")
            print(f"测试群 chat_id：{binding.chat_id}")
            rollout_path = Path(next(event.rollout_path for event in self.database.events_for_rollout(rollout_id) if event.rollout_path))
            while time.time() < deadline:
                try:
                    if reply_token in rollout_path.read_text(encoding="utf-8", errors="ignore"):
                        break
                except OSError:
                    pass
                time.sleep(1)
            else:
                raise TimeoutError("Feishu reply did not enter the same Codex rollout before timeout")

        cleaned = False
        if not keep_resources:
            platform.disband_chat(binding.chat_id)
            self.database.invalidate_binding(rollout_id, binding.chat_id)
            shutil.rmtree(workspace)
            cleaned = True
        self.logger(f"acceptance passed rollout={rollout_id} token={token} cleaned={cleaned}")
        return AcceptanceResult(rollout_id, binding.chat_id, token, reply_token, cleaned)

    @staticmethod
    def _thread_id(stdout: str) -> str:
        for line in stdout.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") in {"thread.started", "thread_started"}:
                return str(event.get("thread_id") or event.get("threadId") or "")
            if event.get("thread_id"):
                return str(event["thread_id"])
        return ""
