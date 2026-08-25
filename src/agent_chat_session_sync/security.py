from __future__ import annotations

import os
from pathlib import Path
import stat


def _current_windows_user_sid():
    import win32api
    import win32security

    token = win32security.OpenProcessToken(
        win32api.GetCurrentProcess(), win32security.TOKEN_QUERY
    )
    try:
        return win32security.GetTokenInformation(token, win32security.TokenUser)[0]
    finally:
        token.Close()


def _windows_private_sids(current_user):
    import win32security

    return (
        current_user,
        win32security.CreateWellKnownSid(win32security.WinLocalSystemSid, None),
        win32security.CreateWellKnownSid(
            win32security.WinBuiltinAdministratorsSid, None
        ),
    )


def _apply_windows_private_dacl(path: Path, inherit: bool) -> None:
    import ntsecuritycon
    import win32security

    current_user = _current_windows_user_sid()
    ace_flags = 0
    if inherit:
        ace_flags = (
            win32security.OBJECT_INHERIT_ACE | win32security.CONTAINER_INHERIT_ACE
        )
    dacl = win32security.ACL()
    for sid in _windows_private_sids(current_user):
        dacl.AddAccessAllowedAceEx(
            win32security.ACL_REVISION,
            ace_flags,
            ntsecuritycon.FILE_ALL_ACCESS,
            sid,
        )
    win32security.SetNamedSecurityInfo(
        str(path),
        win32security.SE_FILE_OBJECT,
        win32security.OWNER_SECURITY_INFORMATION
        | win32security.DACL_SECURITY_INFORMATION
        | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
        current_user,
        None,
        dacl,
        None,
    )


def ensure_private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        _apply_windows_private_dacl(path, inherit=True)
    else:
        path.chmod(0o700)


def harden_private_file(path: Path) -> None:
    if os.name == "nt":
        _apply_windows_private_dacl(path, inherit=False)
    else:
        path.chmod(0o600)


def preserve_file_mode(source: Path, target: Path) -> None:
    if os.name != "nt":
        target.chmod(source.stat().st_mode & 0o777)


def audit_private_path(path: Path) -> list[tuple[str, bool, str]]:
    try:
        info = path.stat()
    except OSError as exc:
        return [("exists", False, f"{path}: {exc.strerror or exc}")]
    if os.name != "nt":
        expected_mode = 0o700 if stat.S_ISDIR(info.st_mode) else 0o600
        actual_mode = stat.S_IMODE(info.st_mode)
        return [
            ("owner", info.st_uid == os.getuid(), f"uid={info.st_uid}"),
            (
                "mode",
                actual_mode == expected_mode,
                f"mode={actual_mode:04o}; expected {expected_mode:04o}",
            ),
        ]

    try:
        return _audit_windows_private_path(path)
    except Exception as exc:
        return [("ACL audit", False, f"{path}: {exc}")]


def _audit_windows_private_path(path: Path) -> list[tuple[str, bool, str]]:
    import ntsecuritycon
    import win32security

    descriptor = win32security.GetNamedSecurityInfo(
        str(path),
        win32security.SE_FILE_OBJECT,
        win32security.OWNER_SECURITY_INFORMATION
        | win32security.DACL_SECURITY_INFORMATION,
    )
    current_user = _current_windows_user_sid()
    expected_sids = {
        str(sid) for sid in _windows_private_sids(current_user)
    }
    owner = descriptor.GetSecurityDescriptorOwner()
    dacl = descriptor.GetSecurityDescriptorDacl()
    control, _revision = descriptor.GetSecurityDescriptorControl()
    if dacl is None:
        return [
            ("owner", str(owner) == str(current_user), "owner=current-user"),
            ("principals", False, "DACL is missing"),
            ("access", False, "DACL is missing"),
            ("inheritance", False, "DACL is missing"),
        ]

    entries = [dacl.GetAce(index) for index in range(dacl.GetAceCount())]
    allowed_entries = [
        entry
        for entry in entries
        if entry[0][0] == win32security.ACCESS_ALLOWED_ACE_TYPE
    ]
    actual_sids = {str(entry[2]) for entry in allowed_entries}
    protected = bool(control & win32security.SE_DACL_PROTECTED)
    if path.is_dir():
        expected_flags = (
            win32security.OBJECT_INHERIT_ACE | win32security.CONTAINER_INHERIT_ACE
        )
        inheritance_ok = protected and all(
            entry[0][1] == expected_flags for entry in allowed_entries
        )
    else:
        explicit_ok = protected and all(
            entry[0][1] == 0 for entry in allowed_entries
        )
        inherited_ok = (
            not protected
            and all(
                entry[0][1] == win32security.INHERITED_ACE
                for entry in allowed_entries
            )
            and all(okay for _suffix, okay, _detail in audit_private_path(path.parent))
        )
        inheritance_ok = explicit_ok or inherited_ok
    return [
        ("owner", str(owner) == str(current_user), "owner=current-user"),
        (
            "principals",
            len(entries) == len(allowed_entries) == 3
            and actual_sids == expected_sids,
            "expected current user, SYSTEM, and Administrators only",
        ),
        (
            "access",
            all(entry[1] == ntsecuritycon.FILE_ALL_ACCESS for entry in allowed_entries),
            "expected full access for each allowed principal",
        ),
        (
            "inheritance",
            inheritance_ok,
            "expected protected ACL or safe inheritance from a private directory",
        ),
    ]
