from __future__ import annotations

import json
from pathlib import Path
import tempfile
import time
import unittest

from agent_chat_session_sync.config import Settings
from agent_chat_session_sync.platforms.feishu import FeishuPlatform
from agent_chat_session_sync.queue import EventDatabase
from agent_chat_session_sync.worker import EventWorker

from tests.test_resolver import write_rollout


class FakeBridge:
    def __init__(self, failures: int = 0):
        self.failures = failures
        self.attached = []

    def attach_agent_session(self, *args):
        if self.failures:
            self.failures -= 1
            raise ConnectionRefusedError("cc-connect socket unavailable")
        self.attached.append(args)
        return {"status": "ok"}


class FakePlatform:
    user_open_id = "ou_user"

    def __init__(self, send_failures: int = 0):
        self.send_failures = send_failures
        self.created: dict[tuple[str, int], str] = {}
        self.sent: list[tuple[str, str, str]] = []

    def validate_chat(self, chat_id):
        return bool(chat_id)

    def create_session_chat(self, session_id, cwd, generation, title):
        return self.created.setdefault((session_id, generation), f"oc_{session_id[-4:]}_{generation}")

    def session_key(self, chat_id):
        return f"feishu:{chat_id}:ou_user"

    def rename_chat(self, chat_id, title):
        return None

    def send_message(self, chat_id, text, idempotency_key=""):
        self.sent.append((chat_id, text, idempotency_key))
        if self.send_failures:
            self.send_failures -= 1
            raise TimeoutError("Feishu timeout")
        return f"om_{len(self.sent)}"


def make_settings(root: Path) -> tuple[Settings, Path]:
    data = root / "data"
    codex_home = root / ".codex"
    work = root / "work"
    work.mkdir()
    config = root / "config.toml"
    config.write_text(
        f'''[[projects]]
name = "project"
mode = "multi-workspace"
base_dir = "{root}"
workspace_init_allow_local_paths = true
[projects.agent]
type = "codex"
[[projects.platforms]]
type = "feishu"
[projects.platforms.options]
''',
        encoding="utf-8",
    )
    settings = Settings(data, config, root / "api.sock", codex_home, root / ".claude")
    return settings, work


def due(database: EventDatabase, event_id: int) -> None:
    event = database.get_event(event_id)
    database.transition(event_id, event.state, next_attempt_at=0)


class WorkerTests(unittest.TestCase):
    def make_worker(self, settings: Settings, platform: FakePlatform, bridge: FakeBridge) -> EventWorker:
        worker = EventWorker(settings, lambda _: None, platform_factory=lambda _: platform)
        worker.coordinator.bridge = bridge
        worker.claude_coordinator.bridge = bridge
        return worker

    @staticmethod
    def enable_claude_project(settings: Settings, root: Path) -> None:
        with settings.cc_config.open("a", encoding="utf-8") as handle:
            handle.write(
                f'''\n[[projects]]
name = "claude-project"
mode = "multi-workspace"
base_dir = "{root}"
[projects.agent]
type = "claudecode"
[[projects.platforms]]
type = "feishu"
[projects.platforms.options]
'''
            )

    def test_hook_event_reaches_delivered_with_platform_message_id(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            settings, work = make_settings(root)
            rollout_id = "019fab53-93d9-7032-aecf-29b5f9bcc362"
            transcript = write_rollout(settings.codex_home, rollout_id, work, "hello", "turn-1")
            database = EventDatabase(settings.database_path)
            inbox_id, _ = database.enqueue(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "session_id": "temporary",
                    "transcript_path": str(transcript),
                    "cwd": str(work),
                    "prompt": "hello",
                }
            )
            platform = FakePlatform()
            worker = self.make_worker(settings, platform, FakeBridge())

            self.assertTrue(worker.run_once())
            delivered = database.get_event(inbox_id)
            self.assertEqual(delivered.state, "delivered")
            outbox = database.get_outbox(delivered.stable_event_id)
            self.assertEqual(outbox.platform_message_id, "om_1")
            self.assertEqual(platform.sent[0][2], delivered.stable_event_id)

    def test_rollout_delay_is_retried_after_worker_restart(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            settings, work = make_settings(root)
            database = EventDatabase(settings.database_path)
            inbox_id, _ = database.enqueue(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "session_id": "temporary",
                    "cwd": str(work),
                    "prompt": "delayed",
                }
            )
            platform = FakePlatform()
            first = self.make_worker(settings, platform, FakeBridge())
            first.run_once()
            self.assertEqual(database.get_event(inbox_id).state, "resolving_session")

            received = database.get_event(inbox_id).received_at
            write_rollout(
                settings.codex_home,
                "019fab53-93d9-7032-aecf-29b5f9bcc362",
                work,
                "delayed",
                "turn-delay",
                received,
            )
            due(database, inbox_id)
            restarted = self.make_worker(settings, platform, FakeBridge())
            restarted.run_once()
            self.assertEqual(database.get_event(inbox_id).state, "delivered")

    def test_socket_failure_and_feishu_timeout_both_retry_idempotently(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            settings, work = make_settings(root)
            rollout_id = "019fab53-93d9-7032-aecf-29b5f9bcc362"
            transcript = write_rollout(settings.codex_home, rollout_id, work, "hello", "turn-1")
            database = EventDatabase(settings.database_path)
            first_id, _ = database.enqueue(
                {"hook_event_name": "UserPromptSubmit", "session_id": "tmp1", "transcript_path": str(transcript), "cwd": str(work), "prompt": "hello"}
            )
            platform = FakePlatform(send_failures=1)
            worker = self.make_worker(settings, platform, FakeBridge(failures=1))
            worker.run_once()
            self.assertEqual(database.get_event(first_id).state, "sending")

            due(database, first_id)
            worker.coordinator.bridge = FakeBridge()
            worker.run_once()
            self.assertEqual(database.get_event(first_id).state, "sending")

            due(database, first_id)
            worker.run_once()
            delivered = database.get_event(first_id)
            self.assertEqual(delivered.state, "delivered")
            keys = [item[2] for item in platform.sent]
            self.assertEqual(len(set(keys)), 1)

    def test_duplicate_hook_delivery_does_not_duplicate_outbox(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            settings, work = make_settings(root)
            rollout_id = "019fab53-93d9-7032-aecf-29b5f9bcc362"
            transcript = write_rollout(settings.codex_home, rollout_id, work, "hello", "turn-1")
            raw_event = {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "temporary",
                "transcript_path": str(transcript),
                "cwd": str(work),
                "prompt": "hello",
            }
            database = EventDatabase(settings.database_path)
            first, created = database.enqueue(raw_event)
            second, duplicate_created = database.enqueue(raw_event)
            self.assertEqual(first, second)
            self.assertTrue(created)
            self.assertFalse(duplicate_created)
            platform = FakePlatform()
            self.make_worker(settings, platform, FakeBridge()).run_once()
            self.assertEqual(len(platform.sent), 1)

    def test_claude_event_uses_claude_project_and_namespaced_binding(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            settings, work = make_settings(root)
            self.enable_claude_project(settings, root)
            session_id = "a0b1c2d3-1111-2222-3333-444455556666"
            transcript = settings.claude_home / "projects/p" / f"{session_id}.jsonl"
            transcript.parent.mkdir(parents=True)
            transcript.write_text(
                json.dumps({"type": "user", "sessionId": session_id, "cwd": str(work), "message": {"content": "hello"}}) + "\n",
                encoding="utf-8",
            )
            database = EventDatabase(settings.database_path)
            inbox_id, _ = database.enqueue(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "session_id": session_id,
                    "prompt_id": "prompt-1",
                    "transcript_path": str(transcript),
                    "cwd": str(work),
                    "prompt": "hello",
                },
                agent_type="claudecode",
            )
            bridge = FakeBridge()
            platform = FakePlatform()
            self.make_worker(settings, platform, bridge).run_once()
            self.assertEqual(database.get_event(inbox_id).state, "delivered")
            self.assertEqual(bridge.attached[0][0], "claude-project")
            self.assertEqual(database.get_binding(f"claudecode:{session_id}").project, "claude-project")

    def test_worker_replays_bindings_when_cc_connect_socket_changes(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            settings, _work = make_settings(root)
            settings.cc_socket.touch()
            database = EventDatabase(settings.database_path)
            from agent_chat_session_sync.models import Binding

            database.put_binding(
                "claudecode:a0b1c2d3-1111-2222-3333-444455556666",
                Binding("oc_1", "feishu:oc_1:ou_user", "claude-project", str(root), 1, "now", "Claude test"),
            )
            bridge = FakeBridge()
            worker = self.make_worker(settings, FakePlatform(), bridge)
            worker._maybe_replay_bindings()
            self.assertEqual(len(bridge.attached), 1)
            self.assertEqual(bridge.attached[0][2], "a0b1c2d3-1111-2222-3333-444455556666")
            worker._maybe_replay_bindings()
            self.assertEqual(len(bridge.attached), 1)
            settings.cc_socket.unlink()
            settings.cc_socket.touch()
            worker._maybe_replay_bindings()
            self.assertEqual(len(bridge.attached), 2)


if __name__ == "__main__":
    unittest.main()
