import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from agent_chat_session_sync.installer import (
    installed_executable,
    hook_command,
    install_claude_hooks,
    install_codex_hooks,
    uninstall_codex_hooks,
)


class InstallerTests(unittest.TestCase):
    def test_install_preserves_unrelated_hooks_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "hooks.json"
            unrelated = {"hooks": [{"type": "command", "command": "other-hook"}]}
            path.write_text(json.dumps({"hooks": {"Stop": [unrelated]}}), encoding="utf-8")

            install_codex_hooks(path, "agent-chat-session-sync hook")
            install_codex_hooks(path, "agent-chat-session-sync hook")

            document = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(document["hooks"]["Stop"][0], unrelated)
            self.assertEqual(len(document["hooks"]["Stop"]), 2)
            self.assertGreaterEqual(set(document["hooks"]), {"SessionStart", "UserPromptSubmit", "Stop"})

            uninstall_codex_hooks(path)
            document = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(document["hooks"]["Stop"], [unrelated])
            self.assertNotIn("SessionStart", document["hooks"])

    def test_uninstall_recognizes_module_fallback_command(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "hooks.json"
            command = "/usr/bin/python3 -m agent_chat_session_sync hook"
            install_codex_hooks(path, command)
            uninstall_codex_hooks(path)
            document = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(document["hooks"], {})

    def test_uninstall_is_idempotent_when_hook_file_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "missing-hooks.json"
            self.assertEqual(uninstall_codex_hooks(path), path)
            self.assertFalse(path.exists())

    def test_claude_install_preserves_settings_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text(json.dumps({"permissions": {"allow": ["Read"]}}), encoding="utf-8")
            install_claude_hooks(path, "agent-chat-session-sync hook --agent claudecode")
            install_claude_hooks(path, "agent-chat-session-sync hook --agent claudecode")
            document = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(document["permissions"], {"allow": ["Read"]})
            self.assertEqual(len(document["hooks"]["Stop"]), 1)

    def test_installed_executable_uses_current_venv_even_when_path_omits_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bin_dir = Path(directory) / "bin"
            bin_dir.mkdir()
            python = bin_dir / ("python.exe" if os.name == "nt" else "python")
            command = bin_dir / ("agent-chat-session-sync.exe" if os.name == "nt" else "agent-chat-session-sync")
            command.touch(mode=0o755)
            with mock.patch("agent_chat_session_sync.installer.sys.executable", str(python)), mock.patch(
                "agent_chat_session_sync.installer.shutil.which", return_value=None
            ):
                self.assertEqual(installed_executable(), str(command.resolve()))

    @unittest.skipUnless(os.name == "nt", "Windows console-script quoting contract")
    def test_generated_hook_command_executes_from_a_path_with_spaces(self) -> None:
        executable = installed_executable()
        self.assertIsNotNone(executable)
        self.assertIn(" ", str(executable), "test environment must exercise a spaced executable path")
        with tempfile.TemporaryDirectory() as directory:
            environment = dict(os.environ)
            environment["ACSS_DATA_DIR"] = directory
            receipt = subprocess.run(
                hook_command("codex"),
                input=json.dumps({"hook_event_name": "Stop", "session_id": "windows-hook"}),
                text=True,
                capture_output=True,
                env=environment,
                check=False,
            )
            status = subprocess.run(
                [str(executable), "events", "--limit", "1"],
                text=True,
                capture_output=True,
                env=environment,
                check=False,
            )

        self.assertEqual(receipt.returncode, 0, receipt.stderr)
        self.assertEqual(receipt.stdout.strip(), "{}")
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertIn("received", status.stdout)
