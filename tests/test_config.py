import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from agent_chat_session_sync.config import Settings, matching_project
from agent_chat_session_sync.endpoints import windows_default_local_endpoint


class ConfigTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows default data directory")
    def test_windows_default_data_dir_uses_local_app_data(self) -> None:
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
            os.environ, {"LOCALAPPDATA": directory}, clear=False
        ):
            os.environ.pop("ACSS_DATA_DIR", None)
            settings = Settings.from_env()

        self.assertEqual(settings.data_dir, Path(directory) / "agent-chat-session-sync")

    def test_local_endpoint_environment_takes_precedence_over_legacy_socket(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "CC_CONNECT_ENDPOINT": "npipe://./pipe/cc-connect-api-user",
                "CC_CONNECT_SOCKET": "/tmp/legacy.sock",
            },
            clear=False,
        ):
            settings = Settings.from_env()

        self.assertEqual(str(settings.local_endpoint), "npipe://./pipe/cc-connect-api-user")

    @unittest.skipUnless(os.name == "nt", "Windows default local endpoint")
    def test_windows_default_local_endpoint_uses_current_sid(self) -> None:
        import win32security

        from agent_chat_session_sync.security import _current_windows_user_sid

        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CC_CONNECT_ENDPOINT", None)
            settings = Settings.from_env()

        sid = win32security.ConvertSidToStringSid(_current_windows_user_sid())
        self.assertEqual(settings.local_endpoint, windows_default_local_endpoint(sid))

    def test_matching_project_uses_most_specific_workdir(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tmp_path = Path(directory)
            nested = tmp_path / "nested"
            nested.mkdir()
            config = {
                "projects": [
                    {
                        "name": "root",
                        "agent": {"type": "codex", "options": {"work_dir": str(tmp_path)}},
                        "platforms": [{"type": "feishu", "options": {}}],
                    },
                    {
                        "name": "nested",
                        "agent": {"type": "codex", "options": {"work_dir": str(nested)}},
                        "platforms": [{"type": "feishu", "options": {}}],
                    },
                ]
            }
            project = matching_project(config, str(nested))
            self.assertIsNotNone(project)
            self.assertEqual(project.name, "nested")

    def test_multi_workspace_project_matches_from_base_dir(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            workspace = base / "repo"
            workspace.mkdir()
            config = {
                "projects": [{
                    "name": "all-repos",
                    "mode": "multi-workspace",
                    "base_dir": str(base),
                    "agent": {"type": "codex", "options": {"work_dir": str(base / "default")}},
                    "platforms": [{"type": "feishu", "options": {}}],
                }]
            }
            project = matching_project(config, str(workspace))
            self.assertIsNotNone(project)
            self.assertEqual(project.base_dir, str(base.resolve()))
            self.assertEqual(project.mode, "multi-workspace")
