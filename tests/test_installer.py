import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from agent_chat_session_sync.installer import installed_executable, install_codex_hooks, uninstall_codex_hooks


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

    def test_installed_executable_uses_current_venv_even_when_path_omits_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bin_dir = Path(directory) / "bin"
            bin_dir.mkdir()
            python = bin_dir / "python"
            command = bin_dir / "agent-chat-session-sync"
            python.symlink_to(Path(sys.executable))
            command.touch(mode=0o755)
            with mock.patch("agent_chat_session_sync.installer.sys.executable", str(python)), mock.patch(
                "agent_chat_session_sync.installer.shutil.which", return_value=None
            ):
                self.assertEqual(installed_executable(), str(command.resolve()))
