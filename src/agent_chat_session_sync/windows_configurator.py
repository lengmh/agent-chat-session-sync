from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import tomllib
from typing import Any, Callable, Literal
import uuid

from .cc_configurator import FEISHU_OPTIONS_RE, PROJECT_BLOCK_RE
from .endpoints import LocalEndpoint
from .locking import exclusive_file_lock
from .security import (
    ensure_private_directory,
    harden_private_file,
    preserve_file_mode,
)


CheckStatus = Literal["missing", "consistent", "conflict"]
CODEX_MANAGED_OPTIONS: dict[str, str] = {
    "backend": "app_server",
    "app_server_lifecycle": "stdio",
    "app_server_url": "stdio://",
    "permission_profile": "cc-connect-workspace",
}
CODEX_PROFILE_NAME = "cc-connect-workspace"
CODEX_PROFILE_DESCRIPTION = "Remote turns may edit only the active workspace."


@dataclass(frozen=True)
class WindowsConfigCheck:
    name: str
    status: CheckStatus
    detail: str


@dataclass(frozen=True)
class WindowsConfigurationPlan:
    path: Path
    original_exists: bool
    original_bytes: bytes
    original_text: str
    original_sha256: str
    updated_text: str
    checks: tuple[WindowsConfigCheck, ...]

    @property
    def has_conflicts(self) -> bool:
        return any(check.status == "conflict" for check in self.checks)

    @property
    def has_changes(self) -> bool:
        return self.updated_text != self.original_text

    def report_lines(self) -> list[str]:
        return [
            f"{check.status.upper():10} {check.name}: {check.detail}"
            for check in self.checks
        ]


@dataclass(frozen=True)
class WindowsConfigurationApplyResult:
    changed: bool
    backup_dir: Path | None


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    if isinstance(value, dict):
        return "{ " + ", ".join(
            f"{key} = {_toml_value(item)}" for key, item in value.items()
        ) + " }"
    return json.dumps(str(value), ensure_ascii=False)


def _field_check(
    checks: list[WindowsConfigCheck],
    *,
    project_name: str,
    field: str,
    current: Any,
    present: bool,
    desired: Any,
) -> None:
    if not present:
        status: CheckStatus = "missing"
    elif current == desired:
        status = "consistent"
    else:
        status = "conflict"
    checks.append(
        WindowsConfigCheck(
            f"{project_name} {field}",
            status,
            f"project={project_name}; field={field}",
        )
    )


def _coverage_boundary(
    project: dict[str, Any],
    checks: list[WindowsConfigCheck],
) -> Path | None:
    name = str(project.get("name", "<unnamed>"))
    options = project.get("agent", {}).get("options", {})
    raw = options.get("work_dir") or project.get("base_dir")
    if not raw:
        checks.append(
            WindowsConfigCheck(
                f"{name} workspace boundary",
                "conflict",
                f"project={name}; no existing base_dir or work_dir",
            )
        )
        return None
    candidate = Path(str(raw)).expanduser()
    if not candidate.is_absolute():
        checks.append(
            WindowsConfigCheck(
                f"{name} workspace boundary",
                "conflict",
                f"project={name}; workspace boundary must be absolute",
            )
        )
        return None
    try:
        boundary = candidate.resolve()
    except OSError:
        boundary = candidate.absolute()
    if boundary == Path(boundary.anchor):
        checks.append(
            WindowsConfigCheck(
                f"{name} workspace boundary",
                "conflict",
                f"project={name}; filesystem root is outside the safe apply boundary",
            )
        )
        return None
    if not boundary.is_dir():
        checks.append(
            WindowsConfigCheck(
                f"{name} workspace boundary",
                "conflict",
                f"project={name}; workspace boundary does not exist",
            )
        )
        return None
    checks.append(
        WindowsConfigCheck(
            f"{name} workspace boundary",
            "consistent",
            f"project={name}; boundary={boundary}",
        )
    )
    return boundary


def _ensure_root_key(block: str, key: str, value: Any) -> str:
    nested = re.search(r"(?m)^\[projects\.", block)
    end = nested.start() if nested else len(block)
    root = block[:end]
    if re.search(rf"(?m)^\s*{re.escape(key)}\s*=", root):
        return block
    separator = "" if root.endswith("\n") else "\n"
    return root + separator + f"{key} = {_toml_value(value)}\n" + block[end:]


def _ensure_agent_options_section(block: str) -> str:
    if re.search(r"(?m)^\[projects\.agent\.options\]\s*$", block):
        return block
    platform = re.search(r"(?m)^\[\[projects\.platforms\]\]\s*$", block)
    insertion = platform.start() if platform else len(block)
    before = block[:insertion]
    separator = "" if before.endswith("\n") else "\n"
    return (
        before
        + separator
        + "[projects.agent.options]\n"
        + block[insertion:]
    )


def _ensure_section_key(block: str, header: str, key: str, value: Any) -> str:
    pattern = re.compile(rf"(?m)^\[{re.escape(header)}\]\s*$")
    match = pattern.search(block)
    if match is None:
        raise ValueError(f"TOML section not found: [{header}]")
    next_header = re.search(r"(?m)^\[", block[match.end() :])
    end = match.end() + next_header.start() if next_header else len(block)
    section = block[match.start() : end]
    if re.search(rf"(?m)^\s*{re.escape(key)}\s*=", section):
        return block
    before = block[:end]
    separator = "" if before.endswith("\n") else "\n"
    return before + separator + f"{key} = {_toml_value(value)}\n" + block[end:]


def _enable_feishu_binding_routing(block: str, app_ids: set[str]) -> str:
    def replace(match: re.Match[str]) -> str:
        platform = tomllib.loads("[[projects]]\n" + match.group(0))["projects"][0][
            "platforms"
        ][0]
        app_id = str(platform.get("options", {}).get("app_id", ""))
        if app_id not in app_ids:
            return match.group(0)
        body = match.group("body")
        if re.search(r"(?m)^\s*binding_routing\s*=", body):
            return match.group(0)
        separator = "" if body.endswith("\n") else "\n"
        return (
            match.group("head")
            + body
            + separator
            + "binding_routing = true\n"
        )

    return FEISHU_OPTIONS_RE.sub(replace, block)


def _render_claude_project(
    source: dict[str, Any],
    boundary: Path,
    *,
    name: str,
) -> str:
    platform = next(
        item
        for item in source.get("platforms", [])
        if str(item.get("type", "")).lower() == "feishu"
    )
    options = dict(platform.get("options", {}))
    options["group_reply_all"] = True
    options["binding_routing"] = True
    lines = [
        "",
        "[[projects]]",
        f"name = {_toml_value(name)}",
        'mode = "multi-workspace"',
        f"base_dir = {_toml_value(str(boundary))}",
        "workspace_init_allow_local_paths = true",
        "",
        "[projects.agent]",
        'type = "claudecode"',
        "",
        "[projects.agent.options]",
        'mode = "auto"',
        "",
        "[[projects.platforms]]",
        'type = "feishu"',
        "",
        "[projects.platforms.options]",
    ]
    lines.extend(f"{key} = {_toml_value(value)}" for key, value in options.items())
    return "\n".join(lines) + "\n"


def _ensure_top_level_key(text: str, key: str, value: Any) -> str:
    first_project = re.search(r"(?m)^\[\[projects\]\]\s*$", text)
    end = first_project.start() if first_project else len(text)
    prefix = text[:end]
    if re.search(rf"(?m)^\s*{re.escape(key)}\s*=", prefix):
        return text
    separator = "" if prefix.endswith("\n") or not prefix else "\n"
    return (
        prefix
        + separator
        + f"{key} = {_toml_value(value)}\n\n"
        + text[end:]
    )


def _section_span(text: str, headers: tuple[str, ...]) -> tuple[int, int] | None:
    header_pattern = "|".join(re.escape(header) for header in headers)
    match = re.search(rf"(?m)^\[(?:{header_pattern})\]\s*$", text)
    if match is None:
        return None
    next_header = re.search(r"(?m)^\[", text[match.end() :])
    end = match.end() + next_header.start() if next_header else len(text)
    return match.start(), end


def _ensure_named_section_key(
    text: str,
    headers: tuple[str, ...],
    key: str,
    value: Any,
) -> str:
    span = _section_span(text, headers)
    if span is None:
        raise ValueError(f"TOML section not found: {headers[0]}")
    start, end = span
    section = text[start:end]
    if re.search(rf"(?m)^\s*{re.escape(key)}\s*=", section):
        return text
    before = text[:end]
    separator = "" if before.endswith("\n") else "\n"
    return before + separator + f"{key} = {_toml_value(value)}\n" + text[end:]


def plan_codex_permission_profile(path: Path) -> WindowsConfigurationPlan:
    try:
        original_bytes = path.read_bytes()
        original_exists = True
    except FileNotFoundError:
        original_bytes = b""
        original_exists = False
    if original_bytes.startswith(b"\xef\xbb\xbf"):
        raise RuntimeError(f"refusing UTF-8 BOM in Codex config: {path}")
    original = original_bytes.decode("utf-8")
    config = tomllib.loads(original)
    permissions = config.get("permissions", {})
    profile = permissions.get(CODEX_PROFILE_NAME)
    checks: list[WindowsConfigCheck] = []
    if profile is not None and not isinstance(profile, dict):
        checks.append(
            WindowsConfigCheck(
                "Codex permission profile",
                "conflict",
                f"profile={CODEX_PROFILE_NAME}; expected a TOML table",
            )
        )
        profile = {}

    profile_table = profile if isinstance(profile, dict) else {}
    _field_check(
        checks,
        project_name="Codex permission profile",
        field="description",
        current=profile_table.get("description"),
        present="description" in profile_table,
        desired=CODEX_PROFILE_DESCRIPTION,
    )
    _field_check(
        checks,
        project_name="Codex permission profile",
        field="extends",
        current=profile_table.get("extends"),
        present="extends" in profile_table,
        desired=":workspace",
    )
    network = profile_table.get("network", {})
    if network is not None and not isinstance(network, dict):
        checks.append(
            WindowsConfigCheck(
                "Codex permission profile network",
                "conflict",
                f"profile={CODEX_PROFILE_NAME}; network must be a TOML table",
            )
        )
        network = {}
    network_table = network if isinstance(network, dict) else {}
    _field_check(
        checks,
        project_name="Codex permission profile",
        field="network.enabled",
        current=network_table.get("enabled"),
        present="enabled" in network_table,
        desired=False,
    )

    updated = original
    profile_headers = (
        f"permissions.{CODEX_PROFILE_NAME}",
        f'permissions."{CODEX_PROFILE_NAME}"',
    )
    network_headers = (
        f"permissions.{CODEX_PROFILE_NAME}.network",
        f'permissions."{CODEX_PROFILE_NAME}".network',
    )
    if profile is None:
        separator = "" if not updated or updated.endswith("\n") else "\n"
        updated = (
            updated
            + separator
            + f"\n[permissions.{CODEX_PROFILE_NAME}]\n"
            + f"description = {_toml_value(CODEX_PROFILE_DESCRIPTION)}\n"
            + 'extends = ":workspace"\n'
            + f"\n[permissions.{CODEX_PROFILE_NAME}.network]\n"
            + "enabled = false\n"
        )
    else:
        updated = _ensure_named_section_key(
            updated,
            profile_headers,
            "description",
            CODEX_PROFILE_DESCRIPTION,
        )
        updated = _ensure_named_section_key(
            updated,
            profile_headers,
            "extends",
            ":workspace",
        )
        if _section_span(updated, network_headers) is None:
            separator = "" if updated.endswith("\n") else "\n"
            updated += (
                separator
                + f"\n[permissions.{CODEX_PROFILE_NAME}.network]\n"
                + "enabled = false\n"
            )
        else:
            updated = _ensure_named_section_key(
                updated,
                network_headers,
                "enabled",
                False,
            )
    tomllib.loads(updated)
    return WindowsConfigurationPlan(
        path=path,
        original_exists=original_exists,
        original_bytes=original_bytes,
        original_text=original,
        original_sha256=hashlib.sha256(original_bytes).hexdigest(),
        updated_text=updated,
        checks=tuple(checks),
    )


def plan_windows_configuration(
    path: Path,
    endpoint: LocalEndpoint | None = None,
) -> WindowsConfigurationPlan:
    if endpoint is not None and endpoint.transport != "npipe":
        raise ValueError("configure-windows requires npipe local endpoint")
    original_bytes = path.read_bytes()
    if original_bytes.startswith(b"\xef\xbb\xbf"):
        raise RuntimeError(f"refusing UTF-8 BOM in cc-connect config: {path}")
    original = original_bytes.decode("utf-8")
    config = tomllib.loads(original)
    checks: list[WindowsConfigCheck] = []
    projects = list(config.get("projects", []))
    if endpoint is not None:
        _field_check(
            checks,
            project_name="cc-connect",
            field="internal_api_endpoint",
            current=config.get("internal_api_endpoint"),
            present="internal_api_endpoint" in config,
            desired=str(endpoint),
        )
    elif "internal_api_endpoint" in config:
        try:
            configured_endpoint = LocalEndpoint.parse(
                str(config["internal_api_endpoint"])
            )
        except ValueError:
            configured_endpoint = None
        if configured_endpoint is None or configured_endpoint.transport != "npipe":
            checks.append(
                WindowsConfigCheck(
                    "cc-connect internal_api_endpoint",
                    "conflict",
                    "configure-windows requires an npipe endpoint",
                )
            )
    names = [str(project.get("name", "")) for project in projects]
    if not names or any(not name for name in names) or len(names) != len(set(names)):
        checks.append(
            WindowsConfigCheck(
                "project identities",
                "conflict",
                "project names must be present and unique",
            )
        )

    managed: dict[str, tuple[dict[str, Any], Path, str]] = {}
    unsupported_project_names: set[str] = set()
    project_blocks: dict[str, str] = {}
    for match in PROJECT_BLOCK_RE.finditer(original):
        block = match.group(0)
        parsed = tomllib.loads(block)["projects"][0]
        project_blocks[str(parsed.get("name", ""))] = block
    for project in projects:
        platforms = [
            platform
            for platform in project.get("platforms", [])
            if str(platform.get("type", "")).lower() == "feishu"
        ]
        if not platforms:
            continue
        name = str(project.get("name", "<unnamed>"))
        agent = project.get("agent", {})
        agent_type = str(agent.get("type", "codex")).lower()
        if agent_type not in {"codex", "claudecode"}:
            continue
        block = project_blocks.get(name, "")
        agent_options = agent.get("options", {})
        if agent_options and not re.search(
            r"(?m)^\[projects\.agent\.options\]\s*$",
            block,
        ):
            unsupported_project_names.add(name)
            checks.append(
                WindowsConfigCheck(
                    f"{name} agent options syntax",
                    "conflict",
                    f"project={name}; unsupported inline/dotted agent options",
                )
            )
        if any(
            platform.get("options")
            and not re.search(
                r"(?m)^\[projects\.platforms\.options\]\s*$",
                block,
            )
            for platform in platforms
        ):
            unsupported_project_names.add(name)
            checks.append(
                WindowsConfigCheck(
                    f"{name} platform options syntax",
                    "conflict",
                    f"project={name}; unsupported inline/dotted platform options",
                )
            )
        if agent_type in managed:
            checks.append(
                WindowsConfigCheck(
                    f"{agent_type} project",
                    "conflict",
                    f"multiple Feishu-backed {agent_type} projects require manual selection",
                )
            )
            continue
        boundary = _coverage_boundary(project, checks)
        if boundary is None:
            continue
        managed[agent_type] = (project, boundary, name)

    if "codex" not in managed:
        checks.append(
            WindowsConfigCheck(
                "codex project",
                "conflict",
                "exactly one Feishu-backed codex project is required",
            )
        )
    if "claudecode" not in managed:
        if "codex" in managed and "local-claude" not in names:
            checks.append(
                WindowsConfigCheck(
                    "claudecode project",
                    "missing",
                    "a local-claude project will be added within the Codex workspace boundary",
                )
            )
        else:
            checks.append(
                WindowsConfigCheck(
                    "claudecode project",
                    "conflict",
                    "exactly one Feishu-backed claudecode project is required",
                )
            )
    app_projects: dict[str, set[str]] = {}
    for project, _boundary, name in managed.values():
        for platform in project.get("platforms", []):
            if str(platform.get("type", "")).lower() != "feishu":
                continue
            app_id = str(platform.get("options", {}).get("app_id", ""))
            if app_id:
                app_projects.setdefault(app_id, set()).add(name)
    shared_routing_app_ids = {
        app_id
        for app_id, project_names in app_projects.items()
        if len(project_names) > 1
    }
    for agent_type, (project, boundary, name) in managed.items():
        _field_check(
            checks,
            project_name=name,
            field="mode",
            current=project.get("mode"),
            present="mode" in project,
            desired="multi-workspace",
        )
        current_base = project.get("base_dir")
        normalized_base = ""
        if current_base:
            try:
                normalized_base = str(Path(str(current_base)).expanduser().resolve())
            except OSError:
                normalized_base = str(current_base)
        _field_check(
            checks,
            project_name=name,
            field="base_dir",
            current=normalized_base,
            present="base_dir" in project,
            desired=str(boundary),
        )
        _field_check(
            checks,
            project_name=name,
            field="workspace_init_allow_local_paths",
            current=project.get("workspace_init_allow_local_paths"),
            present="workspace_init_allow_local_paths" in project,
            desired=True,
        )
        for platform in project.get("platforms", []):
            if str(platform.get("type", "")).lower() != "feishu":
                continue
            options = platform.get("options", {})
            app_id = str(options.get("app_id", ""))
            if app_id in shared_routing_app_ids:
                _field_check(
                    checks,
                    project_name=name,
                    field="binding_routing",
                    current=options.get("binding_routing"),
                    present="binding_routing" in options,
                    desired=True,
                )
        if agent_type == "codex":
            options = project.get("agent", {}).get("options", {})
            for legacy in ("sandbox", "approval_policy", "approvalPolicy"):
                if legacy in options:
                    checks.append(
                        WindowsConfigCheck(
                            f"{name} legacy {legacy}",
                            "conflict",
                            f"project={name}; remove legacy permission override manually",
                        )
                    )
            if str(options.get("app_server_socket", "")).strip():
                checks.append(
                    WindowsConfigCheck(
                        f"{name} app_server_socket",
                        "conflict",
                        f"project={name}; stdio lifecycle cannot retain a daemon socket",
                    )
                )
            for field, desired in CODEX_MANAGED_OPTIONS.items():
                _field_check(
                    checks,
                    project_name=name,
                    field=field,
                    current=options.get(field),
                    present=field in options,
                    desired=desired,
                )

    def update_block(match: re.Match[str]) -> str:
        block = match.group(0)
        parsed = tomllib.loads(block)["projects"][0]
        name = str(parsed.get("name", ""))
        selected = next(
            (
                (agent_type, boundary)
                for agent_type, (_project, boundary, project_name) in managed.items()
                if project_name == name
            ),
            None,
        )
        if selected is None:
            return block
        agent_type, boundary = selected
        if name in unsupported_project_names:
            return block
        block = _ensure_root_key(block, "mode", "multi-workspace")
        block = _ensure_root_key(block, "base_dir", str(boundary))
        block = _ensure_root_key(
            block,
            "workspace_init_allow_local_paths",
            True,
        )
        block = _enable_feishu_binding_routing(block, shared_routing_app_ids)
        if agent_type == "codex":
            block = _ensure_agent_options_section(block)
            for field, value in CODEX_MANAGED_OPTIONS.items():
                block = _ensure_section_key(
                    block,
                    "projects.agent.options",
                    field,
                    value,
                )
        return block

    updated = PROJECT_BLOCK_RE.sub(update_block, original)
    if endpoint is not None:
        updated = _ensure_top_level_key(
            updated,
            "internal_api_endpoint",
            str(endpoint),
        )
    if "claudecode" not in managed and "codex" in managed and "local-claude" not in names:
        source, boundary, _name = managed["codex"]
        updated = updated.rstrip() + "\n" + _render_claude_project(
            source,
            boundary,
            name="local-claude",
        )
    tomllib.loads(updated)
    return WindowsConfigurationPlan(
        path=path,
        original_exists=True,
        original_bytes=original_bytes,
        original_text=original,
        original_sha256=hashlib.sha256(original_bytes).hexdigest(),
        updated_text=updated,
        checks=tuple(checks),
    )


def _write_private_bytes(path: Path, content: bytes) -> None:
    ensure_private_directory(path.parent)
    path.write_bytes(content)
    harden_private_file(path)


def _atomic_replace_config(path: Path, content: bytes) -> None:
    if not path.parent.exists():
        ensure_private_directory(path.parent)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    temporary = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if path.exists():
            preserve_file_mode(path, temporary)
        harden_private_file(temporary)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def apply_windows_configuration(
    plan: WindowsConfigurationPlan,
    data_dir: Path,
    *,
    additional_plans: tuple[WindowsConfigurationPlan, ...] = (),
    before_write: Callable[[], None] | None = None,
    write_file: Callable[[Path, bytes], None] = _atomic_replace_config,
) -> WindowsConfigurationApplyResult:
    plans = (plan, *additional_plans)
    if any(item.has_conflicts for item in plans):
        raise RuntimeError("refusing to apply conflicting Windows configuration")
    changed_plans = tuple(item for item in plans if item.has_changes)
    if not changed_plans:
        return WindowsConfigurationApplyResult(False, None)

    ensure_private_directory(data_dir)
    with exclusive_file_lock(data_dir / "configure-windows.lock", blocking=False):
        for item in changed_plans:
            current_exists = item.path.exists()
            current = item.path.read_bytes() if current_exists else b""
            if (
                current_exists != item.original_exists
                or hashlib.sha256(current).hexdigest() != item.original_sha256
            ):
                raise RuntimeError(
                    f"{item.path} changed after inspection; "
                    "rerun configure-windows --check"
                )

        backups = data_dir / "backups"
        ensure_private_directory(backups)
        operation_id = (
            datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            + "-"
            + uuid.uuid4().hex
        )
        backup_dir = backups / operation_id
        ensure_private_directory(backup_dir)
        artifacts: list[dict[str, Any]] = []
        for index, item in enumerate(changed_plans):
            if index == 0:
                backup_name = "config.toml"
            elif index == 1:
                backup_name = "codex-config.toml"
            else:
                backup_name = f"config-{index + 1}.toml"
            if item.original_exists:
                _write_private_bytes(backup_dir / backup_name, item.original_bytes)
            artifacts.append(
                {
                    "path": str(item.path.resolve()),
                    "existed": item.original_exists,
                    "backup": backup_name if item.original_exists else "",
                    "original_sha256": item.original_sha256,
                }
            )
        manifest_path = backup_dir / "manifest.json"
        manifest: dict[str, Any] = {
            "operation_id": operation_id,
            "config_path": str(plan.path.resolve()),
            "original_sha256": plan.original_sha256,
            "artifacts": artifacts,
            "status": "snapshot",
        }
        _write_private_bytes(
            manifest_path,
            (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode(
                "utf-8"
            ),
        )

        if before_write is not None:
            before_write()
        for item in changed_plans:
            current_exists = item.path.exists()
            current = item.path.read_bytes() if current_exists else b""
            if (
                current_exists != item.original_exists
                or hashlib.sha256(current).hexdigest() != item.original_sha256
            ):
                raise RuntimeError(
                    f"{item.path} changed after inspection; "
                    "rerun configure-windows --check"
                )

        written: list[WindowsConfigurationPlan] = []
        try:
            for item in changed_plans:
                updated_bytes = item.updated_text.encode("utf-8")
                tomllib.loads(updated_bytes.decode("utf-8"))
                write_file(item.path, updated_bytes)
                written.append(item)
            manifest["status"] = "applied"
            for artifact, item in zip(artifacts, changed_plans, strict=True):
                artifact["updated_sha256"] = hashlib.sha256(
                    item.updated_text.encode("utf-8")
                ).hexdigest()
            _write_private_bytes(
                manifest_path,
                (
                    json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
                ).encode("utf-8"),
            )
        except Exception:
            rollback_errors: list[str] = []
            for item in reversed(written):
                try:
                    if item.original_exists:
                        _atomic_replace_config(item.path, item.original_bytes)
                    else:
                        item.path.unlink(missing_ok=True)
                except Exception as rollback_exc:
                    rollback_errors.append(f"{item.path}: {rollback_exc}")
            manifest["status"] = (
                "rollback_failed" if rollback_errors else "rolled_back"
            )
            if rollback_errors:
                manifest["rollback_errors"] = rollback_errors
            _write_private_bytes(
                manifest_path,
                (
                    json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
                ).encode("utf-8"),
            )
            if rollback_errors:
                raise RuntimeError(
                    "Windows configuration apply failed and rollback was incomplete: "
                    + "; ".join(rollback_errors)
                )
            raise
        return WindowsConfigurationApplyResult(True, backup_dir)
