from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import stat
from typing import Any


@dataclass(frozen=True)
class SecurityCheck:
    name: str
    okay: bool
    detail: str


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
