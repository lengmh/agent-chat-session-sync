import json
from pathlib import Path
import tempfile
import unittest

from agent_chat_session_sync.models import Binding
from agent_chat_session_sync.store import BindingStore


def binding(chat_id: str = "oc_live", generation: int = 1) -> Binding:
    return Binding(chat_id, f"feishu:{chat_id}:ou_user", "project", "/work", generation, "now")


class StoreTests(unittest.TestCase):
    def test_store_round_trip_and_compare_invalidate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            store = BindingStore(path)
            store.bind("session", binding())
            self.assertEqual(BindingStore(path).get("session"), binding())
            self.assertFalse(store.invalidate("session", "oc_other"))
            self.assertIsNotNone(store.get("session"))
            self.assertTrue(store.invalidate("session", "oc_live"))
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["sessions"], {})
