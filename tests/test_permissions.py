from __future__ import annotations

import os
from pathlib import Path
import socket
import tempfile
import unittest
from unittest import mock

from agent_chat_session_sync.endpoints import LocalEndpoint
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


@unittest.skipUnless(os.name == "nt", "Windows Named Pipe security contract")
class NamedPipeSecurityTests(unittest.TestCase):
    def test_restricted_named_pipe_dacl_passes(self) -> None:
        import ntsecuritycon
        import win32security

        from agent_chat_session_sync.permissions import local_endpoint_security_checks
        from agent_chat_session_sync.security import (
            _current_windows_user_sid,
            _windows_private_sids,
        )

        current = _current_windows_user_sid()
        dacl = win32security.ACL()
        for sid in _windows_private_sids(current):
            dacl.AddAccessAllowedAceEx(
                win32security.ACL_REVISION,
                0,
                ntsecuritycon.FILE_ALL_ACCESS,
                sid,
            )
        descriptor = mock.Mock()
        descriptor.GetSecurityDescriptorOwner.return_value = current
        descriptor.GetSecurityDescriptorDacl.return_value = dacl
        descriptor.GetSecurityDescriptorControl.return_value = (
            win32security.SE_DACL_PROTECTED,
            1,
        )

        with mock.patch(
            "win32security.GetNamedSecurityInfo",
            return_value=descriptor,
        ) as get_security:
            checks = local_endpoint_security_checks(
                "cc-connect endpoint",
                LocalEndpoint("npipe", "./pipe/cc-connect-api-test"),
            )

        self.assertTrue(all(check.okay for check in checks), checks)
        self.assertEqual(
            get_security.call_args.args[0],
            r"\\.\pipe\cc-connect-api-test",
        )


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
