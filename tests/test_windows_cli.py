from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from agent_chat_session_sync import __version__
from agent_chat_session_sync.provenance import Provenance
from agent_chat_session_sync.security import ensure_private_directory


@unittest.skipUnless(os.name == "nt", "Windows CLI contract")
class WindowsCLITests(unittest.TestCase):
    def test_version_runs_without_importing_unix_only_modules(self) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "agent_chat_session_sync", "--version"],
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), __version__)
        self.assertEqual(result.stderr, "")

    def test_doctor_fails_closed_instead_of_crashing_on_unsupported_endpoint(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            config = root / "config.toml"
            config.write_text("projects = []\n", encoding="utf-8")
            environment = os.environ.copy()
            environment.update(
                {
                    "ACSS_DATA_DIR": str(root / "data"),
                    "CC_CONNECT_CONFIG": str(config),
                    "CC_CONNECT_SOCKET": str(root / "missing-api.sock"),
                    "CODEX_HOME": str(root / ".codex"),
                    "CLAUDE_HOME": str(root / ".claude"),
                }
            )
            result = subprocess.run(
                [sys.executable, "-m", "agent_chat_session_sync", "doctor"],
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertIn("FAIL  cc-connect", result.stdout)

    def test_doctor_reports_unsafe_data_directory_acl(self) -> None:
        import ntsecuritycon
        import win32security

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            data = root / "data"
            data.mkdir()
            everyone = win32security.CreateWellKnownSid(win32security.WinWorldSid, None)
            unsafe = win32security.ACL()
            unsafe.AddAccessAllowedAceEx(
                win32security.ACL_REVISION,
                win32security.OBJECT_INHERIT_ACE | win32security.CONTAINER_INHERIT_ACE,
                ntsecuritycon.FILE_ALL_ACCESS,
                everyone,
            )
            win32security.SetNamedSecurityInfo(
                str(data),
                win32security.SE_FILE_OBJECT,
                win32security.DACL_SECURITY_INFORMATION
                | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
                None,
                None,
                unsafe,
                None,
            )
            config = root / "config.toml"
            config.write_text("projects = []\n", encoding="utf-8")
            environment = os.environ.copy()
            environment.update(
                {
                    "ACSS_DATA_DIR": str(data),
                    "CC_CONNECT_CONFIG": str(config),
                    "CC_CONNECT_SOCKET": str(root / "missing-api.sock"),
                    "CODEX_HOME": str(root / ".codex"),
                    "CLAUDE_HOME": str(root / ".claude"),
                }
            )
            result = subprocess.run(
                [sys.executable, "-m", "agent_chat_session_sync", "doctor"],
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("FAIL  data directory principals:", result.stdout)

    def test_doctor_reports_unsafe_runtime_file_acl(self) -> None:
        import ntsecuritycon
        import win32security

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            data = root / "data"
            ensure_private_directory(data)
            runtime_log = data / "sync.log"
            runtime_log.touch()
            everyone = win32security.CreateWellKnownSid(win32security.WinWorldSid, None)
            unsafe = win32security.ACL()
            unsafe.AddAccessAllowedAceEx(
                win32security.ACL_REVISION,
                0,
                ntsecuritycon.FILE_ALL_ACCESS,
                everyone,
            )
            win32security.SetNamedSecurityInfo(
                str(runtime_log),
                win32security.SE_FILE_OBJECT,
                win32security.DACL_SECURITY_INFORMATION
                | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
                None,
                None,
                unsafe,
                None,
            )
            config = root / "config.toml"
            config.write_text("projects = []\n", encoding="utf-8")
            environment = os.environ.copy()
            environment.update(
                {
                    "ACSS_DATA_DIR": str(data),
                    "CC_CONNECT_CONFIG": str(config),
                    "CC_CONNECT_SOCKET": str(root / "missing-api.sock"),
                    "CODEX_HOME": str(root / ".codex"),
                    "CLAUDE_HOME": str(root / ".claude"),
                }
            )
            result = subprocess.run(
                [sys.executable, "-m", "agent_chat_session_sync", "doctor"],
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("FAIL  runtime log principals:", result.stdout)

    def test_verify_install_parses_unquoted_windows_executable_path(self) -> None:
        from agent_chat_session_sync import cli

        identity = Provenance(
            service_version=__version__,
            git_commit="commit-a",
            package_path=r"C:\venv\Lib\site-packages\agent_chat_session_sync",
            python_path=r"C:\venv\Scripts\python.exe",
            build_source="git:commit-a",
        )
        hook_command = (
            r"C:\venv\Scripts\agent-chat-session-sync.exe hook --agent codex"
        )
        hook_document = {
            "hooks": {
                "Stop": [{"hooks": [{"type": "command", "command": hook_command}]}]
            }
        }
        probe_commands: list[list[str]] = []

        def run(command, **_kwargs):
            if command[:2] == ["git", "status"]:
                return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
            probe_commands.append(command)
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=json.dumps(identity.to_dict()),
                stderr="",
            )

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            hooks = root / "hooks.json"
            claude = root / "settings.json"
            hooks.write_text(json.dumps(hook_document), encoding="utf-8")
            claude.write_text(json.dumps(hook_document), encoding="utf-8")
            with mock.patch(
                "agent_chat_session_sync.cli.source_head", return_value="commit-a"
            ), mock.patch(
                "agent_chat_session_sync.cli.current_provenance",
                return_value=identity,
            ), mock.patch(
                "agent_chat_session_sync.cli.subprocess.run", side_effect=run
            ):
                result = cli.main(
                    [
                        "verify-install",
                        "--source",
                        str(root),
                        "--expected-commit",
                        "commit-a",
                        "--hooks-file",
                        str(hooks),
                        "--claude-settings-file",
                        str(claude),
                    ]
                )

        self.assertEqual(result, 0)
        self.assertEqual(
            probe_commands,
            [
                [
                    r"C:\venv\Scripts\agent-chat-session-sync.exe",
                    "provenance",
                    "--json",
                ],
                [
                    r"C:\venv\Scripts\agent-chat-session-sync.exe",
                    "provenance",
                    "--json",
                ],
            ],
        )


if __name__ == "__main__":
    unittest.main()
