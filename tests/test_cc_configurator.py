from pathlib import Path
import tempfile
import tomllib
import unittest

from agent_chat_session_sync.cc_configurator import configure_claude_project, rename_agent_projects


class CCConfiguratorTests(unittest.TestCase):
    def test_clones_feishu_secret_without_printing_or_duplicate_projects(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "config.toml"
            path.write_text(
                '''[[projects]]
name = "codex"
mode = "multi-workspace"
base_dir = "/"
[projects.agent]
type = "codex"
[[projects.platforms]]
type = "feishu"
[projects.platforms.options]
app_id = "cli_id"
app_secret = "private"
allow_from = "ou_user"
''',
                encoding="utf-8",
            )
            backup, name, created = configure_claude_project(path)
            self.assertTrue(created)
            self.assertEqual(name, "local-claude")
            self.assertTrue(backup.is_file())
            config = tomllib.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(len(config["projects"]), 2)
            for project in config["projects"]:
                options = project["platforms"][0]["options"]
                self.assertTrue(options["binding_routing"])
                self.assertEqual(options["app_secret"], "private")
            _backup, _name, created_again = configure_claude_project(path)
            self.assertFalse(created_again)
            self.assertEqual(len(tomllib.loads(path.read_text())["projects"]), 2)

    def test_renames_agent_projects_without_rewriting_options(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "config.toml"
            path.write_text(
                '''[[projects]]
name = "legacy"
[projects.agent]
type = "codex"
[[projects.platforms]]
type = "feishu"
[projects.platforms.options]
app_secret = "private"

[[projects]]
name = "legacy-claude"
[projects.agent]
type = "claudecode"
[[projects.platforms]]
type = "feishu"
[projects.platforms.options]
app_secret = "private"
''',
                encoding="utf-8",
            )
            backup, renamed = rename_agent_projects(path)
            self.assertTrue(backup.is_file())
            self.assertEqual(
                renamed,
                {"legacy": "local-codex", "legacy-claude": "local-claude"},
            )
            config = tomllib.loads(path.read_text())
            self.assertEqual([item["name"] for item in config["projects"]], ["local-codex", "local-claude"])
            self.assertTrue(all(item["platforms"][0]["options"]["app_secret"] == "private" for item in config["projects"]))


if __name__ == "__main__":
    unittest.main()
