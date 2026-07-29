from pathlib import Path
import tempfile
import unittest

from agent_chat_session_sync.agents.codex import CodexAdapter
from agent_chat_session_sync.coordinator import SessionCoordinator
from agent_chat_session_sync.errors import PlatformAPIError
from agent_chat_session_sync.models import AgentEvent
from agent_chat_session_sync.store import BindingStore


class FakeBridge:
    def __init__(self):
        self.attached = []

    def attach_agent_session(self, *args):
        self.attached.append(args)
        return {"status": "ok"}


class FakePlatform:
    user_open_id = "ou_user"

    def __init__(self, available=True):
        self.available = available
        self.availability = []
        self.created = []
        self.sent = []
        self.fail_chat = None
        self.renamed = []

    def validate_chat(self, chat_id):
        if self.availability:
            return self.availability.pop(0)
        return self.available if chat_id == "oc_old" else True

    def create_session_chat(self, session_id, cwd, generation, title):
        chat_id = f"oc_new_{generation}"
        self.created.append((chat_id, generation))
        return chat_id

    def session_key(self, chat_id):
        return f"feishu:{chat_id}:ou_user"

    def send_message(self, chat_id, text, idempotency_key=""):
        self.sent.append((chat_id, text))
        if chat_id == self.fail_chat:
            raise PlatformAPIError(230001, "gone")
        return f"om_{len(self.sent)}"

    def rename_chat(self, chat_id, title):
        self.renamed.append((chat_id, title))


def make_coordinator(tmp_path: Path, platform: FakePlatform):
    rollout_id = "019f92ba-d95e-7e02-b81b-ddee818481b9"
    transcript = tmp_path / f"rollout-x-{rollout_id}.jsonl"
    transcript.write_text("{}", encoding="utf-8")
    work = tmp_path / "work"
    work.mkdir()
    event = AgentEvent("UserPromptSubmit", "temporary", str(work), str(transcript), "hello")
    config = {
        "projects": [{
            "name": "project",
            "agent": {"type": "codex", "options": {"work_dir": str(work)}},
            "platforms": [{"type": "feishu", "options": {}}],
        }]
    }
    store = BindingStore(tmp_path / "state.json")
    bridge = FakeBridge()
    coordinator = SessionCoordinator(
        CodexAdapter(tmp_path / ".codex", lambda _: None), bridge, store, config, lambda _: None,
        platform_factory=lambda _: platform,
    )
    return rollout_id, event, store, bridge, coordinator


class CoordinatorTests(unittest.TestCase):
    def test_creates_binds_and_sends(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tmp_path = Path(directory)
            platform = FakePlatform()
            rollout_id, event, store, bridge, coordinator = make_coordinator(tmp_path, platform)
            result = coordinator.handle(event)
            self.assertEqual(result.chat_id, "oc_new_1")
            self.assertEqual(store.get(rollout_id).chat_id, "oc_new_1")
            self.assertEqual(len(bridge.attached), 1)
            self.assertEqual(platform.sent[-1], ("oc_new_1", "🧑 本地 Codex\n\nhello"))

    def test_stale_mapping_gets_new_generation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tmp_path = Path(directory)
            platform = FakePlatform(available=False)
            rollout_id, event, store, _, coordinator = make_coordinator(tmp_path, platform)
            from agent_chat_session_sync.models import Binding
            store.bind(rollout_id, Binding("oc_old", "key", "project", event.cwd, 1, "now"))
            result = coordinator.handle(event)
            self.assertEqual(result.chat_id, "oc_new_2")
            self.assertEqual(result.generation, 2)

    def test_send_race_rebuilds_and_resends(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tmp_path = Path(directory)
            platform = FakePlatform(available=True)
            rollout_id, event, store, _, coordinator = make_coordinator(tmp_path, platform)
            from agent_chat_session_sync.models import Binding
            store.bind(rollout_id, Binding("oc_old", "key", "project", event.cwd, 1, "now"))
            platform.fail_chat = "oc_old"
            platform.availability = [True, False]
            result = coordinator.handle(event)
            self.assertEqual(result.chat_id, "oc_new_2")
            self.assertEqual(platform.sent[-1], ("oc_new_2", "🧑 本地 Codex\n\nhello"))

    def test_existing_chat_is_renamed_when_codex_title_appears(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tmp_path = Path(directory)
            platform = FakePlatform(available=True)
            rollout_id, event, store, _, coordinator = make_coordinator(tmp_path, platform)
            from agent_chat_session_sync.models import Binding
            store.bind(rollout_id, Binding("oc_old", "key", "project", event.cwd, 1, "now"))
            coordinator.agent.session_index.parent.mkdir(parents=True)
            coordinator.agent.session_index.write_text(
                '{"id":"%s","thread_name":"绘画任务"}\n' % rollout_id, encoding="utf-8"
            )
            start_event = AgentEvent("SessionStart", event.session_id, event.cwd, event.transcript_path)

            result = coordinator.handle(start_event)

            self.assertEqual(platform.renamed, [("oc_old", "绘画任务")])
            self.assertEqual(result.title, "绘画任务")
            self.assertEqual(store.get(rollout_id).title, "绘画任务")
