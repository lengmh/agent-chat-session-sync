from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from agent_chat_session_sync import __version__


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


if __name__ == "__main__":
    unittest.main()
