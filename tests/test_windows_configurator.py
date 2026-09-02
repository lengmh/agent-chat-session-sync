from pathlib import Path
import tempfile
import tomllib
import unittest

from agent_chat_session_sync.endpoints import LocalEndpoint
from agent_chat_session_sync.windows_configurator import (
    apply_windows_configuration,
    plan_codex_permission_profile,
    plan_windows_configuration,
)


class WindowsConfiguratorTests(unittest.TestCase):
    def test_windows_configuration_rejects_unix_endpoint(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "config.toml"
            path.write_text("projects = []\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "requires npipe"):
                plan_windows_configuration(
                    path,
                    LocalEndpoint("unix", "/tmp/cc-connect.sock"),
                )

    def test_inline_options_are_conflicts_instead_of_partial_updates(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            workspace = root / "workspace"
            workspace.mkdir()
            path = root / "config.toml"
            path.write_text(
                f'''[[projects]]
name = "local-codex"
[projects.agent]
type = "codex"
options = {{ work_dir = "{workspace.as_posix()}" }}
[[projects.platforms]]
type = "feishu"
options = {{ app_id = "cli_sensitive", app_secret = "super-secret" }}
''',
                encoding="utf-8",
            )

            plan = plan_windows_configuration(path)

            self.assertTrue(plan.has_conflicts)
            self.assertIn("unsupported inline/dotted", "\n".join(plan.report_lines()))

    def test_codex_permission_profile_is_added_without_rewriting_other_settings(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "config.toml"
            path.write_text(
                'model = "gpt-5.3-codex"\n',
                encoding="utf-8",
            )

            plan = plan_codex_permission_profile(path)

            self.assertFalse(plan.has_conflicts)
            self.assertTrue(plan.has_changes)
            config = tomllib.loads(plan.updated_text)
            self.assertEqual(config["model"], "gpt-5.3-codex")
            profile = config["permissions"]["cc-connect-workspace"]
            self.assertEqual(profile["extends"], ":workspace")
            self.assertFalse(profile["network"]["enabled"])

    def test_check_plans_missing_fields_without_exposing_feishu_secret(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            workspace = root / "workspace"
            workspace.mkdir()
            path = root / "config.toml"
            path.write_text(
                f'''[[projects]]
name = "local-codex"
[projects.agent]
type = "codex"
[projects.agent.options]
work_dir = "{workspace.as_posix()}"
[[projects.platforms]]
type = "feishu"
[projects.platforms.options]
app_id = "cli_sensitive"
app_secret = "super-secret"

[[projects]]
name = "local-claude"
[projects.agent]
type = "claudecode"
[projects.agent.options]
work_dir = "{workspace.as_posix()}"
[[projects.platforms]]
type = "feishu"
[projects.platforms.options]
app_id = "cli_sensitive"
app_secret = "super-secret"
''',
                encoding="utf-8",
            )

            plan = plan_windows_configuration(
                path,
                LocalEndpoint("npipe", "./pipe/cc-connect-api-test"),
            )

            report = "\n".join(plan.report_lines())
            self.assertNotIn("super-secret", report)
            self.assertNotIn("cli_sensitive", report)
            self.assertTrue(plan.has_changes)
            self.assertFalse(plan.has_conflicts)
            config = tomllib.loads(plan.updated_text)
            self.assertEqual(
                config["internal_api_endpoint"],
                "npipe://./pipe/cc-connect-api-test",
            )
            codex, claude = config["projects"]
            self.assertEqual(codex["mode"], "multi-workspace")
            self.assertEqual(codex["base_dir"], str(workspace.resolve()))
            self.assertTrue(codex["workspace_init_allow_local_paths"])
            self.assertEqual(codex["agent"]["options"]["backend"], "app_server")
            self.assertEqual(
                codex["agent"]["options"]["app_server_lifecycle"],
                "stdio",
            )
            self.assertEqual(codex["agent"]["options"]["app_server_url"], "stdio://")
            self.assertEqual(
                codex["agent"]["options"]["permission_profile"],
                "cc-connect-workspace",
            )
            self.assertTrue(codex["platforms"][0]["options"]["binding_routing"])
            self.assertTrue(claude["platforms"][0]["options"]["binding_routing"])
            self.assertEqual(path.read_text(encoding="utf-8").count("super-secret"), 2)

    def test_check_allows_independent_feishu_apps(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            workspace = root / "workspace"
            workspace.mkdir()
            path = root / "config.toml"
            path.write_text(
                f'''[[projects]]
name = "local-codex"
[projects.agent]
type = "codex"
[projects.agent.options]
work_dir = "{workspace.as_posix()}"
[[projects.platforms]]
type = "feishu"
[projects.platforms.options]
app_id = "codex-app"
app_secret = "codex-secret"

[[projects]]
name = "local-claude"
[projects.agent]
type = "claudecode"
[projects.agent.options]
work_dir = "{workspace.as_posix()}"
[[projects.platforms]]
type = "feishu"
[projects.platforms.options]
app_id = "claude-app"
app_secret = "claude-secret"
''',
                encoding="utf-8",
            )

            plan = plan_windows_configuration(path)

            report = "\n".join(plan.report_lines())
            self.assertFalse(plan.has_conflicts, report)
            self.assertNotIn("shared Feishu routing", report)
            updated = tomllib.loads(plan.updated_text)
            codex, claude = updated["projects"]
            self.assertEqual(codex["platforms"][0]["options"]["app_id"], "codex-app")
            self.assertEqual(claude["platforms"][0]["options"]["app_id"], "claude-app")
            self.assertEqual(
                codex["platforms"][0]["options"]["app_secret"],
                "codex-secret",
            )
            self.assertEqual(
                claude["platforms"][0]["options"]["app_secret"],
                "claude-secret",
            )

    def test_check_does_not_enable_shared_routing_for_independent_apps(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            workspace = root / "workspace"
            workspace.mkdir()
            path = root / "config.toml"
            path.write_text(
                f'''internal_api_endpoint = "npipe://./pipe/cc-connect-api-test"

[[projects]]
name = "local-codex"
mode = "multi-workspace"
base_dir = "{workspace.as_posix()}"
workspace_init_allow_local_paths = true
[projects.agent]
type = "codex"
[projects.agent.options]
work_dir = "{workspace.as_posix()}"
backend = "app_server"
app_server_lifecycle = "stdio"
app_server_url = "stdio://"
permission_profile = "cc-connect-workspace"
[[projects.platforms]]
type = "feishu"
[projects.platforms.options]
app_id = "codex-app"
app_secret = "codex-secret"

[[projects]]
name = "local-claude"
mode = "multi-workspace"
base_dir = "{workspace.as_posix()}"
workspace_init_allow_local_paths = true
[projects.agent]
type = "claudecode"
[projects.agent.options]
work_dir = "{workspace.as_posix()}"
[[projects.platforms]]
type = "feishu"
[projects.platforms.options]
app_id = "claude-app"
app_secret = "claude-secret"
''',
                encoding="utf-8",
            )

            plan = plan_windows_configuration(path)

            report = "\n".join(plan.report_lines())
            self.assertFalse(plan.has_conflicts, report)
            self.assertFalse(plan.has_changes, report)
            self.assertNotIn("binding_routing", report)
            self.assertNotIn("binding_routing", plan.updated_text)

    def test_check_can_add_claude_project_with_existing_workspace_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            workspace = root / "workspace"
            workspace.mkdir()
            path = root / "config.toml"
            path.write_text(
                f'''[[projects]]
name = "local-codex"
[projects.agent]
type = "codex"
[projects.agent.options]
work_dir = "{workspace.as_posix()}"
[[projects.platforms]]
type = "feishu"
[projects.platforms.options]
app_id = "cli_sensitive"
app_secret = "super-secret"
allow_from = "ou_sensitive"
''',
                encoding="utf-8",
            )

            plan = plan_windows_configuration(path)

            self.assertFalse(plan.has_conflicts)
            config = tomllib.loads(plan.updated_text)
            self.assertEqual(
                [project["agent"]["type"] for project in config["projects"]],
                ["codex", "claudecode"],
            )
            claude = config["projects"][1]
            self.assertEqual(claude["base_dir"], str(workspace.resolve()))
            self.assertEqual(
                claude["platforms"][0]["options"]["app_secret"],
                "super-secret",
            )
            self.assertNotIn("super-secret", "\n".join(plan.report_lines()))

    def test_apply_creates_unique_backup_before_replacing_config(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            workspace = root / "workspace"
            workspace.mkdir()
            path = root / "config.toml"
            original = f'''[[projects]]
name = "local-codex"
[projects.agent]
type = "codex"
[projects.agent.options]
work_dir = "{workspace.as_posix()}"
[[projects.platforms]]
type = "feishu"
[projects.platforms.options]
app_id = "cli_sensitive"
app_secret = "super-secret"

[[projects]]
name = "local-claude"
[projects.agent]
type = "claudecode"
[projects.agent.options]
work_dir = "{workspace.as_posix()}"
[[projects.platforms]]
type = "feishu"
[projects.platforms.options]
app_id = "cli_sensitive"
app_secret = "super-secret"
'''
            path.write_text(original, encoding="utf-8")
            plan = plan_windows_configuration(path)
            codex_path = root / "codex" / "config.toml"
            codex_path.parent.mkdir()
            codex_path.write_text('model = "gpt-5.3-codex"\n', encoding="utf-8")
            codex_plan = plan_codex_permission_profile(codex_path)

            result = apply_windows_configuration(
                plan,
                root / "data",
                additional_plans=(codex_plan,),
            )

            self.assertTrue(result.changed)
            self.assertIsNotNone(result.backup_dir)
            backup_dir = result.backup_dir
            assert backup_dir is not None
            self.assertEqual(
                (backup_dir / "config.toml").read_text(encoding="utf-8"),
                original,
            )
            self.assertTrue((backup_dir / "manifest.json").is_file())
            self.assertEqual(
                (backup_dir / "codex-config.toml").read_text(encoding="utf-8"),
                'model = "gpt-5.3-codex"\n',
            )
            self.assertEqual(path.read_bytes(), plan.updated_text.encode("utf-8"))
            self.assertEqual(
                codex_path.read_bytes(),
                codex_plan.updated_text.encode("utf-8"),
            )

    def test_apply_refuses_concurrent_config_change(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            workspace = root / "workspace"
            workspace.mkdir()
            path = root / "config.toml"
            path.write_text(
                f'''[[projects]]
name = "local-codex"
[projects.agent]
type = "codex"
[projects.agent.options]
work_dir = "{workspace.as_posix()}"
[[projects.platforms]]
type = "feishu"
[projects.platforms.options]
app_id = "cli_sensitive"
app_secret = "super-secret"

[[projects]]
name = "local-claude"
[projects.agent]
type = "claudecode"
[projects.agent.options]
work_dir = "{workspace.as_posix()}"
[[projects.platforms]]
type = "feishu"
[projects.platforms.options]
app_id = "cli_sensitive"
app_secret = "super-secret"
''',
                encoding="utf-8",
            )
            plan = plan_windows_configuration(path)

            with self.assertRaisesRegex(RuntimeError, "changed after inspection"):
                apply_windows_configuration(
                    plan,
                    root / "data",
                    before_write=lambda: path.write_text(
                        path.read_text(encoding="utf-8") + "# concurrent edit\n",
                        encoding="utf-8",
                    ),
                )

            self.assertTrue(path.read_text(encoding="utf-8").endswith("# concurrent edit\n"))
            manifests = list((root / "data" / "backups").glob("*/manifest.json"))
            self.assertEqual(len(manifests), 1)
            self.assertIn('"status": "snapshot"', manifests[0].read_text(encoding="utf-8"))

    def test_apply_rolls_back_first_config_when_second_write_fails(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            workspace = root / "workspace"
            workspace.mkdir()
            cc_path = root / "config.toml"
            cc_original = f'''[[projects]]
name = "local-codex"
[projects.agent]
type = "codex"
[projects.agent.options]
work_dir = "{workspace.as_posix()}"
[[projects.platforms]]
type = "feishu"
[projects.platforms.options]
app_id = "cli_sensitive"
app_secret = "super-secret"

[[projects]]
name = "local-claude"
[projects.agent]
type = "claudecode"
[projects.agent.options]
work_dir = "{workspace.as_posix()}"
[[projects.platforms]]
type = "feishu"
[projects.platforms.options]
app_id = "cli_sensitive"
app_secret = "super-secret"
'''
            cc_path.write_text(cc_original, encoding="utf-8")
            codex_path = root / "codex" / "config.toml"
            codex_path.parent.mkdir()
            codex_original = 'model = "gpt-5.3-codex"\n'
            codex_path.write_text(codex_original, encoding="utf-8")
            cc_plan = plan_windows_configuration(cc_path)
            codex_plan = plan_codex_permission_profile(codex_path)
            writes = 0

            def write_file(path: Path, content: bytes) -> None:
                nonlocal writes
                writes += 1
                if writes == 2:
                    raise RuntimeError("second write failed")
                path.write_bytes(content)

            with self.assertRaisesRegex(RuntimeError, "second write failed"):
                apply_windows_configuration(
                    cc_plan,
                    root / "data",
                    additional_plans=(codex_plan,),
                    write_file=write_file,
                )

            self.assertEqual(cc_path.read_text(encoding="utf-8"), cc_original)
            self.assertEqual(codex_path.read_text(encoding="utf-8"), codex_original)
            manifests = list((root / "data" / "backups").glob("*/manifest.json"))
            self.assertEqual(len(manifests), 1)
            self.assertIn('"status": "rolled_back"', manifests[0].read_text(encoding="utf-8"))

    def test_existing_conflicting_managed_field_is_not_rewritten(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            workspace = root / "workspace"
            workspace.mkdir()
            path = root / "config.toml"
            path.write_text(
                f'''[[projects]]
name = "local-codex"
mode = "multi-workspace"
base_dir = "{workspace.as_posix()}"
workspace_init_allow_local_paths = true
[projects.agent]
type = "codex"
[projects.agent.options]
work_dir = "{workspace.as_posix()}"
backend = "exec"
[[projects.platforms]]
type = "feishu"
[projects.platforms.options]
app_id = "cli_sensitive"
app_secret = "super-secret"
binding_routing = true

[[projects]]
name = "local-claude"
mode = "multi-workspace"
base_dir = "{workspace.as_posix()}"
workspace_init_allow_local_paths = true
[projects.agent]
type = "claudecode"
[projects.agent.options]
work_dir = "{workspace.as_posix()}"
[[projects.platforms]]
type = "feishu"
[projects.platforms.options]
app_id = "cli_sensitive"
app_secret = "super-secret"
binding_routing = true
''',
                encoding="utf-8",
            )

            plan = plan_windows_configuration(path)

            self.assertTrue(plan.has_conflicts)
            self.assertIn('backend = "exec"', plan.updated_text)
            with self.assertRaisesRegex(RuntimeError, "conflicting"):
                apply_windows_configuration(plan, root / "data")


if __name__ == "__main__":
    unittest.main()
