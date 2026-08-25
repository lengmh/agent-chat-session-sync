from __future__ import annotations

import os
from pathlib import Path
import socket
import tempfile
import unittest

from agent_chat_session_sync.permissions import codex_permission_config_checks, socket_security_checks


class PermissionConfigTests(unittest.TestCase):
    def test_native_profile_realtime_config_passes(self) -> None:
        checks = codex_permission_config_checks(
            {
                "projects": [
                    {
                        "name": "all-local",
                        "agent": {
                            "type": "codex",
                            "options": {
                                "backend": "app_server",
                                "app_server_lifecycle": "stdio",
                                "app_server_url": "stdio://",
                                "permission_profile": "workspace-safe",
                            },
                        },
                    }
                ]
            }
        )
        self.assertTrue(all(check.okay for check in checks), checks)

    def test_legacy_exec_config_reports_missing_capabilities(self) -> None:
        checks = codex_permission_config_checks(
            {"projects": [{"name": "legacy", "agent": {"type": "codex", "options": {"mode": "auto-edit"}}}]}
        )
        failed = {check.name for check in checks if not check.okay}
        self.assertIn("legacy App Server backend", failed)

    def test_stdio_url_must_be_explicit(self) -> None:
        checks = codex_permission_config_checks(
            {
                "projects": [
                    {
                        "name": "bad",
                        "agent": {
                            "type": "codex",
                            "options": {
                                "backend": "app_server",
                                "permission_profile": "workspace-safe",
                            },
                        },
                    }
                ]
            }
        )
        self.assertFalse(next(check.okay for check in checks if check.name == "bad stdio transport"))


@unittest.skipUnless(hasattr(socket, "AF_UNIX"), "Unix socket security contract")
class SocketSecurityTests(unittest.TestCase):
    def test_owner_only_socket_passes(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "api.sock"
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                listener.bind(str(path))
                os.chmod(path, 0o600)
                checks = socket_security_checks("test", path)
                self.assertTrue(all(check.okay for check in checks), checks)
            finally:
                listener.close()

    def test_world_access_fails(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "api.sock"
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                listener.bind(str(path))
                os.chmod(path, 0o606)
                checks = socket_security_checks("test", path)
                self.assertFalse(next(check.okay for check in checks if check.name == "test other access"))
            finally:
                listener.close()

    def test_regular_file_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "api.sock"
            path.touch(mode=0o600)
            checks = socket_security_checks("test", path)
            self.assertFalse(next(check.okay for check in checks if check.name == "test type"))


if __name__ == "__main__":
    unittest.main()
