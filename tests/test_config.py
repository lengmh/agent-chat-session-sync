from pathlib import Path
import tempfile
import unittest

from agent_chat_session_sync.config import matching_project


class ConfigTests(unittest.TestCase):
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
