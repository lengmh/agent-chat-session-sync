from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import stat
from typing import Any

from .endpoints import LocalEndpoint
from .security import audit_private_path


@dataclass(frozen=True)
class SecurityCheck:
    name: str
    okay: bool
    detail: str


def private_path_security_checks(name: str, path: Path) -> list[SecurityCheck]:
    return [
        SecurityCheck(f"{name} {suffix}", okay, detail)
        for suffix, okay, detail in audit_private_path(path)
    ]


def socket_security_checks(name: str, path: Path, expected_uid: int | None = None) -> list[SecurityCheck]:
    """Audit a local control socket without trying to change its ownership.

    Owner-only sockets (0600) and intentional owner/group sockets (0660) are
    accepted. Any access for "other" users, unexpected owner, non-socket file,
    or world-writable parent directory fails closed.
    """

    if os.name == "nt":
        return [
            SecurityCheck(
                f"{name} ACL",
                False,
                f"{path}: Windows ACL verification is not available",
            )
        ]

    uid = os.getuid() if expected_uid is None else expected_uid
    try:
        info = path.stat()
    except OSError as exc:
        return [SecurityCheck(name, False, f"{path}: {exc.strerror or exc}")]

    mode = stat.S_IMODE(info.st_mode)
    checks = [
        SecurityCheck(f"{name} type", stat.S_ISSOCK(info.st_mode), f"{path} mode={mode:04o}"),
        SecurityCheck(f"{name} owner", info.st_uid == uid, f"uid={info.st_uid}, service_uid={uid}"),
        SecurityCheck(f"{name} other access", mode & 0o007 == 0, f"mode={mode:04o}; expected 0600 or 0660"),
        SecurityCheck(
            f"{name} owner access",
            mode & 0o600 == 0o600,
            f"mode={mode:04o}; owner must be able to read and write",
        ),
    ]
    if mode & 0o060:
        import grp

        try:
            group_name = grp.getgrgid(info.st_gid).gr_name
        except KeyError:
            group_name = str(info.st_gid)
        groups = set(os.getgroups()) | {os.getgid()}
        checks.append(
            SecurityCheck(
                f"{name} group",
                info.st_gid in groups,
                f"gid={info.st_gid} ({group_name}), service_groups={sorted(groups)}",
            )
        )
    try:
        parent_mode = stat.S_IMODE(path.parent.stat().st_mode)
        parent_safe = parent_mode & 0o002 == 0
        checks.append(
            SecurityCheck(
                f"{name} parent",
                parent_safe,
                f"{path.parent} mode={parent_mode:04o}; parent must not be world-writable",
            )
        )
    except OSError as exc:
        checks.append(SecurityCheck(f"{name} parent", False, str(exc)))
    return checks


def local_endpoint_security_checks(
    name: str,
    endpoint: LocalEndpoint,
) -> list[SecurityCheck]:
    if endpoint.transport == "unix":
        return socket_security_checks(name, Path(endpoint.address))
    if os.name != "nt":
        return [
            SecurityCheck(
                f"{name} ACL",
                False,
                "Windows Named Pipe ACL verification is unavailable on this platform",
            )
        ]
    prefix = "./pipe/"
    if not endpoint.address.startswith(prefix) or endpoint.address == prefix:
        return [
            SecurityCheck(
                f"{name} ACL",
                False,
                f"invalid Windows Named Pipe endpoint: {endpoint}",
            )
        ]

    import ntsecuritycon
    import win32security

    from .security import _current_windows_user_sid, _windows_private_sids

    pipe_path = rf"\\.\pipe\{endpoint.address.removeprefix(prefix)}"
    try:
        descriptor = win32security.GetNamedSecurityInfo(
            pipe_path,
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
    except Exception as exc:
        return [SecurityCheck(f"{name} ACL", False, f"{pipe_path}: {exc}")]
    if dacl is None:
        return [
            SecurityCheck(f"{name} owner", str(owner) == str(current_user), "owner=current-user"),
            SecurityCheck(f"{name} principals", False, "DACL is missing"),
            SecurityCheck(f"{name} access", False, "DACL is missing"),
            SecurityCheck(f"{name} inheritance", False, "DACL is missing"),
        ]

    entries = [dacl.GetAce(index) for index in range(dacl.GetAceCount())]
    allowed_entries = [
        entry
        for entry in entries
        if entry[0][0] == win32security.ACCESS_ALLOWED_ACE_TYPE
    ]
    actual_sids = {str(entry[2]) for entry in allowed_entries}
    return [
        SecurityCheck(
            f"{name} owner",
            str(owner) == str(current_user),
            "expected current user",
        ),
        SecurityCheck(
            f"{name} principals",
            len(entries) == len(allowed_entries) == 3
            and actual_sids == expected_sids,
            "expected current user, SYSTEM, and Administrators only",
        ),
        SecurityCheck(
            f"{name} access",
            all(
                entry[1] == ntsecuritycon.FILE_ALL_ACCESS
                for entry in allowed_entries
            ),
            "expected full access for each allowed principal",
        ),
        SecurityCheck(
            f"{name} inheritance",
            bool(control & win32security.SE_DACL_PROTECTED)
            and all(entry[0][1] == 0 for entry in allowed_entries),
            "expected protected explicit ACL",
        ),
    ]


def codex_permission_config_checks(config: dict[str, Any]) -> list[SecurityCheck]:
    checks: list[SecurityCheck] = []
    for project in config.get("projects", []):
        agent = project.get("agent", {})
        if agent.get("type") != "codex":
            continue
        name = str(project.get("name", "<unnamed>"))
        options = agent.get("options", {})
        backend = str(options.get("backend", "exec")).lower().replace("-", "_")
        lifecycle = str(options.get("app_server_lifecycle", "stdio")).lower()
        profile = str(options.get("permission_profile", "")).strip()
        app_server_url = str(options.get("app_server_url", "")).strip()
        checks.append(
            SecurityCheck(
                f"{name} App Server backend",
                backend == "app_server",
                f"backend={backend}; realtime thread events require app_server",
            )
        )
        if backend != "app_server":
            continue
        checks.append(
            SecurityCheck(
                f"{name} lifecycle",
                lifecycle in {"stdio", "daemon", "shared", "persistent"},
                f"app_server_lifecycle={lifecycle}",
            )
        )
        if lifecycle == "stdio":
            checks.append(
                SecurityCheck(
                    f"{name} stdio transport",
                    app_server_url in {"stdio", "stdio://"},
                    f"app_server_url={app_server_url or '<default>'}; set stdio:// explicitly",
                )
            )
        checks.append(
            SecurityCheck(
                f"{name} permission profile",
                bool(profile),
                f"permission_profile={profile or '<legacy mode fallback>'}",
            )
        )
    return checks
