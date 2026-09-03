from __future__ import annotations

from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from agent_chat_session_sync.acceptance import LiveAcceptance
from agent_chat_session_sync.cli import build_parser
from agent_chat_session_sync.config import Settings


class AcceptanceTests(unittest.TestCase):
    def test_extracts_thread_id_from_current_codex_json(self) -> None:
        output = '\n'.join(
            [
                '{"type":"item.completed","item":{}}',
                '{"type":"thread.started","thread_id":"abc-123"}',
            ]
        )
        self.assertEqual(LiveAcceptance._thread_id(output), "abc-123")

    def test_extracts_legacy_thread_shape_and_ignores_invalid_lines(self) -> None:
        output = 'not-json\n{"type":"thread_started","threadId":"legacy-456"}'
        self.assertEqual(LiveAcceptance._thread_id(output), "legacy-456")

    def test_missing_thread_id_is_empty(self) -> None:
        self.assertEqual(LiveAcceptance._thread_id('{"type":"turn.completed"}'), "")

    def test_acceptance_cli_defaults_to_full_bidirectional_check(self) -> None:
        args = build_parser().parse_args(["acceptance-live"])
        self.assertEqual(args.timeout, 300)
        self.assertFalse(args.keep_resources)
        self.assertFalse(args.skip_reply)

    def test_acceptance_cli_exposes_diagnostic_skip(self) -> None:
        args = build_parser().parse_args(
            ["acceptance-live", "--timeout", "12", "--keep-resources", "--skip-reply"]
        )
        self.assertEqual(args.timeout, 12)
        self.assertTrue(args.keep_resources)
        self.assertTrue(args.skip_reply)

    def test_acceptance_cli_accepts_optional_project_scope(self) -> None:
        args = build_parser().parse_args(
            ["acceptance-live", "--agent", "claudecode", "--project", "playground_cc"]
        )

        self.assertEqual(args.agent, "claudecode")
        self.assertEqual(args.project, "playground_cc")

    def test_scoped_acceptance_rejects_project_for_other_agent(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            workspace = root / "workspace"
            workspace.mkdir()
            config_path = root / "config.toml"
            config_path.write_text(
                f'''[[projects]]
name = "playground_cc"
mode = "multi-workspace"
base_dir = "{workspace.as_posix()}"
[projects.agent]
type = "claudecode"
[projects.agent.options]
work_dir = "{workspace.as_posix()}"
[[projects.platforms]]
type = "feishu"
[projects.platforms.options]
app_id = "claude-app"
app_secret = "claude-secret"
allow_from = "ou_test"
''',
                encoding="utf-8",
            )
            settings = Settings(
                data_dir=root / "data",
                cc_config=config_path,
                cc_socket=root / "api.sock",
                codex_home=root / "codex",
            )

            with self.assertRaisesRegex(RuntimeError, "belongs to claudecode"):
                LiveAcceptance(settings, lambda _message: None).run(
                    project_name="playground_cc",
                    agent_type="codex",
                )

    def test_scoped_acceptance_starts_inside_selected_project_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            # Keep a lexical alias so this assertion checks filesystem identity.
            workspace_alias = root / "workspace-alias"
            workspace_alias.mkdir()
            claude_workspace = workspace_alias / ".." / "claude-workspace"
            codex_workspace = root / "codex-workspace"
            claude_workspace.mkdir()
            codex_workspace.mkdir()
            config_path = root / "config.toml"
            config_path.write_text(
                f'''[[projects]]
name = "playground_cc"
mode = "multi-workspace"
base_dir = "{claude_workspace.as_posix()}"
[projects.agent]
type = "claudecode"
[projects.agent.options]
work_dir = "{claude_workspace.as_posix()}"
[[projects.platforms]]
type = "feishu"
[projects.platforms.options]
app_id = "claude-app"
app_secret = "claude-secret"
allow_from = "ou_test"

[[projects]]
name = "playground_codex"
mode = "multi-workspace"
base_dir = "{codex_workspace.as_posix()}"
[projects.agent]
type = "codex"
[projects.agent.options]
work_dir = "{codex_workspace.as_posix()}"
[[projects.platforms]]
type = "feishu"
[projects.platforms.options]
app_id = "codex-app"
app_secret = "codex-secret"
allow_from = "ou_test"
''',
                encoding="utf-8",
            )
            settings = Settings(
                data_dir=root / "data",
                cc_config=config_path,
                cc_socket=root / "api.sock",
                codex_home=root / "codex",
            )
            calls: list[tuple[list[str], Path]] = []

            def run(command: list[str], *, cwd: Path, **_kwargs: object) -> subprocess.CompletedProcess[str]:
                calls.append((command, cwd))
                return subprocess.CompletedProcess(
                    command,
                    0 if command[:2] == ["git", "init"] else 1,
                    "",
                    "expected test stop",
                )

            with mock.patch(
                "agent_chat_session_sync.acceptance.subprocess.run",
                side_effect=run,
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "claudecode acceptance session failed",
                ):
                    LiveAcceptance(settings, lambda _message: None).run(
                        project_name="playground_cc",
                        agent_type="claudecode",
                    )

            self.assertEqual(len(calls), 2)
            workspace = calls[0][1]
            self.assertEqual(calls[1][1], workspace)
            self.assertTrue(workspace.parent.parent.samefile(claude_workspace))
            self.assertEqual(workspace.parent.name, ".agent-chat-session-sync-acceptance")


if __name__ == "__main__":
    unittest.main()
