from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from agent_chat_session_sync.permissions import private_path_security_checks
from agent_chat_session_sync.security import ensure_private_directory, harden_private_file


@unittest.skipUnless(os.name == "nt", "Windows DACL contract")
class WindowsSecurityTests(unittest.TestCase):
    def test_private_directory_replaces_unsafe_inherited_dacl(self) -> None:
        import ntsecuritycon
        import win32api
        import win32security

        with tempfile.TemporaryDirectory() as raw:
            parent = Path(raw) / "unsafe-parent"
            parent.mkdir()
            everyone = win32security.CreateWellKnownSid(win32security.WinWorldSid, None)
            unsafe = win32security.ACL()
            unsafe.AddAccessAllowedAceEx(
                win32security.ACL_REVISION,
                win32security.OBJECT_INHERIT_ACE | win32security.CONTAINER_INHERIT_ACE,
                ntsecuritycon.FILE_ALL_ACCESS,
                everyone,
            )
            win32security.SetNamedSecurityInfo(
                str(parent),
                win32security.SE_FILE_OBJECT,
                win32security.DACL_SECURITY_INFORMATION
                | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
                None,
                None,
                unsafe,
                None,
            )

            target = parent / "data"
            ensure_private_directory(target)

            descriptor = win32security.GetNamedSecurityInfo(
                str(target),
                win32security.SE_FILE_OBJECT,
                win32security.OWNER_SECURITY_INFORMATION
                | win32security.DACL_SECURITY_INFORMATION,
            )
            owner = descriptor.GetSecurityDescriptorOwner()
            dacl = descriptor.GetSecurityDescriptorDacl()
            control, _revision = descriptor.GetSecurityDescriptorControl()
            token = win32security.OpenProcessToken(
                win32api.GetCurrentProcess(), win32security.TOKEN_QUERY
            )
            current_user = win32security.GetTokenInformation(
                token, win32security.TokenUser
            )[0]
            expected_sids = {
                str(current_user),
                str(
                    win32security.CreateWellKnownSid(
                        win32security.WinLocalSystemSid, None
                    )
                ),
                str(
                    win32security.CreateWellKnownSid(
                        win32security.WinBuiltinAdministratorsSid, None
                    )
                ),
            }
            actual_sids = {
                str(dacl.GetAce(index)[2]) for index in range(dacl.GetAceCount())
            }

        self.assertEqual(str(owner), str(current_user))
        self.assertEqual(actual_sids, expected_sids)
        self.assertTrue(control & win32security.SE_DACL_PROTECTED)

    def test_private_file_replaces_unsafe_inherited_dacl(self) -> None:
        import ntsecuritycon
        import win32api
        import win32security

        with tempfile.TemporaryDirectory() as raw:
            parent = Path(raw) / "unsafe-parent"
            parent.mkdir()
            everyone = win32security.CreateWellKnownSid(win32security.WinWorldSid, None)
            unsafe = win32security.ACL()
            unsafe.AddAccessAllowedAceEx(
                win32security.ACL_REVISION,
                win32security.OBJECT_INHERIT_ACE | win32security.CONTAINER_INHERIT_ACE,
                ntsecuritycon.FILE_ALL_ACCESS,
                everyone,
            )
            win32security.SetNamedSecurityInfo(
                str(parent),
                win32security.SE_FILE_OBJECT,
                win32security.DACL_SECURITY_INFORMATION
                | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
                None,
                None,
                unsafe,
                None,
            )

            target = parent / "events.sqlite3"
            target.touch()
            harden_private_file(target)

            descriptor = win32security.GetNamedSecurityInfo(
                str(target),
                win32security.SE_FILE_OBJECT,
                win32security.OWNER_SECURITY_INFORMATION
                | win32security.DACL_SECURITY_INFORMATION,
            )
            owner = descriptor.GetSecurityDescriptorOwner()
            dacl = descriptor.GetSecurityDescriptorDacl()
            control, _revision = descriptor.GetSecurityDescriptorControl()
            token = win32security.OpenProcessToken(
                win32api.GetCurrentProcess(), win32security.TOKEN_QUERY
            )
            current_user = win32security.GetTokenInformation(
                token, win32security.TokenUser
            )[0]
            expected_sids = {
                str(current_user),
                str(
                    win32security.CreateWellKnownSid(
                        win32security.WinLocalSystemSid, None
                    )
                ),
                str(
                    win32security.CreateWellKnownSid(
                        win32security.WinBuiltinAdministratorsSid, None
                    )
                ),
            }
            actual_sids = {
                str(dacl.GetAce(index)[2]) for index in range(dacl.GetAceCount())
            }

        self.assertEqual(str(owner), str(current_user))
        self.assertEqual(actual_sids, expected_sids)
        self.assertTrue(control & win32security.SE_DACL_PROTECTED)

    def test_private_path_audit_rejects_broad_access(self) -> None:
        import ntsecuritycon
        import win32security

        with tempfile.TemporaryDirectory() as raw:
            target = Path(raw) / "data"
            ensure_private_directory(target)
            everyone = win32security.CreateWellKnownSid(win32security.WinWorldSid, None)
            unsafe = win32security.ACL()
            unsafe.AddAccessAllowedAceEx(
                win32security.ACL_REVISION,
                win32security.OBJECT_INHERIT_ACE | win32security.CONTAINER_INHERIT_ACE,
                ntsecuritycon.FILE_ALL_ACCESS,
                everyone,
            )
            win32security.SetNamedSecurityInfo(
                str(target),
                win32security.SE_FILE_OBJECT,
                win32security.DACL_SECURITY_INFORMATION
                | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
                None,
                None,
                unsafe,
                None,
            )

            checks = private_path_security_checks("data directory", target)

        self.assertFalse(all(check.okay for check in checks), checks)
        self.assertIn(
            "data directory principals",
            {check.name for check in checks if not check.okay},
        )

    def test_private_path_audit_accepts_file_inherited_from_private_directory(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            parent = Path(raw) / "data"
            ensure_private_directory(parent)
            target = parent / "events.sqlite3-wal"
            target.touch()

            checks = private_path_security_checks("SQLite WAL", target)

        self.assertTrue(all(check.okay for check in checks), checks)

    def test_private_path_audit_fails_closed_when_security_descriptor_is_unreadable(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            target = Path(raw) / "data"
            target.mkdir()
            with mock.patch(
                "win32security.GetNamedSecurityInfo",
                side_effect=OSError("security descriptor unavailable"),
            ):
                checks = private_path_security_checks("data directory", target)

        self.assertFalse(all(check.okay for check in checks), checks)
        self.assertIn(
            "data directory ACL audit",
            {check.name for check in checks if not check.okay},
        )


if __name__ == "__main__":
    unittest.main()
