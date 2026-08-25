from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
import tempfile
from typing import Callable, Mapping, Protocol
import uuid
import xml.etree.ElementTree as ET

from .endpoints import windows_default_local_endpoint
from .security import ensure_private_directory, harden_private_file


WINDOWS_WORKER_TASK_PATH = "\\AgentChatSessionSync\\"
WINDOWS_WORKER_TASK_NAME = "Worker"
WINDOWS_WORKER_TASK_URI = f"{WINDOWS_WORKER_TASK_PATH}{WINDOWS_WORKER_TASK_NAME}"
TASK_NAMESPACE = "http://schemas.microsoft.com/windows/2004/02/mit/task"
TASK_DESCRIPTION = (
    "Runs the current user's agent-chat-session-sync durable event worker."
)
WORKER_WRAPPER_MARKER = "# Managed by agent-chat-session-sync"
ENVIRONMENT_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class TaskScheduler(Protocol):
    def export(self) -> str | None: ...

    def register(self, xml: str) -> None: ...

    def start(self) -> None: ...

    def stop_and_wait(self) -> None: ...

    def unregister(self) -> None: ...

    def state(self) -> str | None: ...


class PowerShellTaskScheduler:
    def __init__(
        self,
        powershell: Path,
        *,
        run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ):
        self.powershell = powershell.resolve()
        self.run = run

    def _invoke(
        self,
        script: str,
        *,
        input_text: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return self.run(
            [
                str(self.powershell),
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                script,
            ],
            input=input_text,
            text=True,
            encoding="utf-8",
            capture_output=True,
        )

    def export(self) -> str | None:
        script = (
            "$ErrorActionPreference = 'Stop'; "
            f"$task = Get-ScheduledTask -TaskPath '{WINDOWS_WORKER_TASK_PATH}' "
            f"-TaskName '{WINDOWS_WORKER_TASK_NAME}' -ErrorAction SilentlyContinue; "
            "if ($null -eq $task) { exit 3 }; "
            f"Export-ScheduledTask -TaskPath '{WINDOWS_WORKER_TASK_PATH}' "
            f"-TaskName '{WINDOWS_WORKER_TASK_NAME}'"
        )
        result = self._invoke(script)
        if result.returncode == 3:
            return None
        if result.returncode != 0:
            raise RuntimeError(
                f"failed to inspect Scheduled Task {WINDOWS_WORKER_TASK_URI}"
            )
        return result.stdout

    def register(self, xml: str) -> None:
        script = (
            "$ErrorActionPreference = 'Stop'; "
            "[Console]::InputEncoding = [Text.UTF8Encoding]::new($false); "
            "$xml = [Console]::In.ReadToEnd(); "
            "$service = New-Object -ComObject 'Schedule.Service'; "
            "$service.Connect(); "
            "try { "
            "$null = $service.GetFolder('\\AgentChatSessionSync') "
            "} catch { "
            "$root = $service.GetFolder('\\'); "
            "$null = $root.CreateFolder('AgentChatSessionSync') "
            "}; "
            f"Register-ScheduledTask -TaskPath '{WINDOWS_WORKER_TASK_PATH}' "
            f"-TaskName '{WINDOWS_WORKER_TASK_NAME}' -Xml $xml -Force | Out-Null"
        )
        result = self._invoke(script, input_text=xml)
        if result.returncode != 0:
            raise RuntimeError(
                f"failed to register Scheduled Task {WINDOWS_WORKER_TASK_URI}"
            )

    def start(self) -> None:
        script = (
            "$ErrorActionPreference = 'Stop'; "
            f"Start-ScheduledTask -TaskPath '{WINDOWS_WORKER_TASK_PATH}' "
            f"-TaskName '{WINDOWS_WORKER_TASK_NAME}'"
        )
        if self._invoke(script).returncode != 0:
            raise RuntimeError(f"failed to start Scheduled Task {WINDOWS_WORKER_TASK_URI}")

    def stop_and_wait(self) -> None:
        script = (
            "$ErrorActionPreference = 'Stop'; "
            f"$task = Get-ScheduledTask -TaskPath '{WINDOWS_WORKER_TASK_PATH}' "
            f"-TaskName '{WINDOWS_WORKER_TASK_NAME}' -ErrorAction SilentlyContinue; "
            "if ($null -eq $task) { exit 0 }; "
            "if ($task.State -in @('Running', 'Queued')) { "
            f"Stop-ScheduledTask -TaskPath '{WINDOWS_WORKER_TASK_PATH}' "
            f"-TaskName '{WINDOWS_WORKER_TASK_NAME}' "
            "}; "
            "$deadline = [DateTime]::UtcNow.AddSeconds(10); "
            "do { "
            "Start-Sleep -Milliseconds 250; "
            f"$task = Get-ScheduledTask -TaskPath '{WINDOWS_WORKER_TASK_PATH}' "
            f"-TaskName '{WINDOWS_WORKER_TASK_NAME}' -ErrorAction SilentlyContinue; "
            "if ($null -eq $task -or $task.State -notin @('Running', 'Queued')) { exit 0 } "
            "} while ([DateTime]::UtcNow -lt $deadline); "
            "exit 5"
        )
        result = self._invoke(script)
        if result.returncode == 5:
            raise RuntimeError(
                f"timed out stopping Scheduled Task {WINDOWS_WORKER_TASK_URI}"
            )
        if result.returncode != 0:
            raise RuntimeError(
                f"failed to stop Scheduled Task {WINDOWS_WORKER_TASK_URI}"
            )

    def unregister(self) -> None:
        script = (
            "$ErrorActionPreference = 'Stop'; "
            f"$task = Get-ScheduledTask -TaskPath '{WINDOWS_WORKER_TASK_PATH}' "
            f"-TaskName '{WINDOWS_WORKER_TASK_NAME}' -ErrorAction SilentlyContinue; "
            "if ($null -eq $task) { exit 0 }; "
            f"Unregister-ScheduledTask -TaskPath '{WINDOWS_WORKER_TASK_PATH}' "
            f"-TaskName '{WINDOWS_WORKER_TASK_NAME}' -Confirm:$false"
        )
        if self._invoke(script).returncode != 0:
            raise RuntimeError(
                f"failed to unregister Scheduled Task {WINDOWS_WORKER_TASK_URI}"
            )

    def state(self) -> str | None:
        script = (
            "$ErrorActionPreference = 'Stop'; "
            f"$task = Get-ScheduledTask -TaskPath '{WINDOWS_WORKER_TASK_PATH}' "
            f"-TaskName '{WINDOWS_WORKER_TASK_NAME}' -ErrorAction SilentlyContinue; "
            "if ($null -eq $task) { exit 3 }; "
            "[Console]::Out.Write($task.State.ToString())"
        )
        result = self._invoke(script)
        if result.returncode == 3:
            return None
        if result.returncode != 0:
            raise RuntimeError(
                f"failed to query Scheduled Task {WINDOWS_WORKER_TASK_URI}"
            )
        return result.stdout.strip()


def _task_element(parent: ET.Element, name: str, text: str | None = None) -> ET.Element:
    element = ET.SubElement(parent, f"{{{TASK_NAMESPACE}}}{name}")
    if text is not None:
        element.text = text
    return element


def worker_task_arguments(wrapper: Path) -> str:
    return subprocess.list2cmdline(
        [
            "-WindowStyle",
            "Hidden",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(wrapper.resolve()),
        ]
    )


def _powershell_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def render_worker_wrapper(
    *,
    executable: Path,
    environment: Mapping[str, str],
) -> str:
    executable = executable.resolve()
    lines = [
        WORKER_WRAPPER_MARKER,
        "$ErrorActionPreference = 'Stop'",
        "$PSNativeCommandUseErrorActionPreference = $false",
    ]
    for name, value in sorted(environment.items()):
        if not ENVIRONMENT_NAME_RE.fullmatch(name):
            raise ValueError(f"invalid environment variable name: {name}")
        lines.append(f"$env:{name} = {_powershell_literal(str(value))}")
    lines.extend(
        [
            f"Set-Location -LiteralPath {_powershell_literal(str(executable.parent))}",
            "while ($true) {",
            f"    & {_powershell_literal(str(executable))} worker",
            "    $exitCode = $LASTEXITCODE",
            "    if ($exitCode -eq 0 -or $exitCode -eq 4) {",
            "        exit $exitCode",
            "    }",
            "    Start-Sleep -Seconds 10",
            "}",
            "",
        ]
    )
    return "\n".join(lines)


def is_managed_worker_wrapper(content: str) -> bool:
    lines = content.splitlines()
    if len(lines) < 12 or lines[:3] != [
        WORKER_WRAPPER_MARKER,
        "$ErrorActionPreference = 'Stop'",
        "$PSNativeCommandUseErrorActionPreference = $false",
    ]:
        return False
    index = 3
    environment_names: set[str] = set()
    literal = r"'(?P<value>(?:[^']|'')*)'"
    environment_pattern = re.compile(
        rf"^\$env:(?P<name>[A-Za-z_][A-Za-z0-9_]*) = {literal}$"
    )
    while index < len(lines):
        match = environment_pattern.fullmatch(lines[index])
        if match is None:
            break
        name = match.group("name")
        if name in environment_names:
            return False
        environment_names.add(name)
        index += 1
    tail = lines[index:]
    if len(tail) != 9:
        return False
    location_match = re.fullmatch(
        rf"Set-Location -LiteralPath {literal}",
        tail[0],
    )
    executable_match = re.fullmatch(
        rf"    & {literal} worker",
        tail[2],
    )
    if location_match is None or executable_match is None:
        return False
    expected_tail = [
        "while ($true) {",
        None,
        "    $exitCode = $LASTEXITCODE",
        "    if ($exitCode -eq 0 -or $exitCode -eq 4) {",
        "        exit $exitCode",
        "    }",
        "    Start-Sleep -Seconds 10",
        "}",
    ]
    if tail[1] != expected_tail[0] or tail[3:] != expected_tail[2:]:
        return False
    location = location_match.group("value").replace("''", "'")
    executable = executable_match.group("value").replace("''", "'")
    return _normalized_path(location) == _normalized_path(Path(executable).parent)


def windows_worker_environment(
    data_dir: Path,
    *,
    user_sid: str,
) -> dict[str, str]:
    return {
        "ACSS_DATA_DIR": str(data_dir.resolve()),
        "CODEX_HOME": os.environ.get("CODEX_HOME", str(Path.home() / ".codex")),
        "CLAUDE_HOME": os.environ.get("CLAUDE_HOME", str(Path.home() / ".claude")),
        "CC_CONNECT_CONFIG": os.environ.get(
            "CC_CONNECT_CONFIG",
            str(Path.home() / ".cc-connect/config.toml"),
        ),
        "CC_CONNECT_ENDPOINT": os.environ.get("CC_CONNECT_ENDPOINT")
        or str(windows_default_local_endpoint(user_sid)),
    }


def render_worker_task_xml(
    *,
    user_sid: str,
    powershell: Path,
    wrapper: Path,
) -> str:
    if not user_sid.strip():
        raise ValueError("current user SID is required")
    powershell = powershell.resolve()
    wrapper = wrapper.resolve()

    ET.register_namespace("", TASK_NAMESPACE)
    task = ET.Element(f"{{{TASK_NAMESPACE}}}Task", {"version": "1.4"})
    registration = _task_element(task, "RegistrationInfo")
    _task_element(registration, "Description", TASK_DESCRIPTION)
    _task_element(registration, "URI", WINDOWS_WORKER_TASK_URI)

    triggers = _task_element(task, "Triggers")
    logon = _task_element(triggers, "LogonTrigger")
    _task_element(logon, "Enabled", "true")
    _task_element(logon, "UserId", user_sid)

    principals = _task_element(task, "Principals")
    principal = _task_element(principals, "Principal")
    principal.set("id", "CurrentUser")
    _task_element(principal, "UserId", user_sid)
    _task_element(principal, "LogonType", "InteractiveToken")
    _task_element(principal, "RunLevel", "LeastPrivilege")

    settings = _task_element(task, "Settings")
    _task_element(settings, "MultipleInstancesPolicy", "IgnoreNew")
    _task_element(settings, "DisallowStartIfOnBatteries", "false")
    _task_element(settings, "StopIfGoingOnBatteries", "false")
    _task_element(settings, "AllowHardTerminate", "true")
    _task_element(settings, "StartWhenAvailable", "true")
    _task_element(settings, "RunOnlyIfNetworkAvailable", "false")
    _task_element(settings, "AllowStartOnDemand", "true")
    _task_element(settings, "Enabled", "true")
    _task_element(settings, "Hidden", "false")
    _task_element(settings, "ExecutionTimeLimit", "PT0S")
    _task_element(settings, "Priority", "7")

    actions = _task_element(task, "Actions")
    actions.set("Context", "CurrentUser")
    action = _task_element(actions, "Exec")
    _task_element(action, "Command", str(powershell))
    _task_element(action, "Arguments", worker_task_arguments(wrapper))
    _task_element(action, "WorkingDirectory", str(wrapper.parent))
    return ET.tostring(task, encoding="unicode", xml_declaration=True)


def _normalized_path(value: str | Path) -> str:
    return os.path.normcase(os.path.normpath(str(value)))


def task_matches_worker_identity(
    xml: str,
    *,
    user_sid: str,
    powershell: Path,
    wrapper: Path,
) -> bool:
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return False
    prefix = f"{{{TASK_NAMESPACE}}}"
    if root.tag != prefix + "Task":
        return False

    def direct(parent: ET.Element, name: str) -> list[ET.Element]:
        return parent.findall(prefix + name)

    def only(parent: ET.Element, name: str) -> ET.Element | None:
        items = direct(parent, name)
        return items[0] if len(items) == 1 else None

    def value(parent: ET.Element | None, name: str) -> str:
        if parent is None:
            return ""
        item = only(parent, name)
        return (item.text or "").strip() if item is not None else ""

    registration = only(root, "RegistrationInfo")
    triggers = only(root, "Triggers")
    principals = only(root, "Principals")
    settings = only(root, "Settings")
    actions = only(root, "Actions")
    if None in {registration, triggers, principals, settings, actions}:
        return False
    assert registration is not None
    assert triggers is not None
    assert principals is not None
    assert settings is not None
    assert actions is not None
    if len(list(triggers)) != 1 or len(list(principals)) != 1 or len(list(actions)) != 1:
        return False
    logon = only(triggers, "LogonTrigger")
    principal = only(principals, "Principal")
    action = only(actions, "Exec")
    if logon is None or principal is None or action is None:
        return False
    if principal.get("id") != "CurrentUser" or actions.get("Context") != "CurrentUser":
        return False
    if {item.tag for item in logon} != {
        prefix + "Enabled",
        prefix + "UserId",
    }:
        return False
    if {item.tag for item in principal} != {
        prefix + "UserId",
        prefix + "LogonType",
        prefix + "RunLevel",
    }:
        return False
    if {item.tag for item in action} != {
        prefix + "Command",
        prefix + "Arguments",
        prefix + "WorkingDirectory",
    }:
        return False

    expected_arguments = worker_task_arguments(wrapper)
    return (
        value(registration, "URI") == WINDOWS_WORKER_TASK_URI
        and value(logon, "Enabled").lower() == "true"
        and value(logon, "UserId") == user_sid
        and value(principal, "UserId") == user_sid
        and value(principal, "LogonType") == "InteractiveToken"
        and value(principal, "RunLevel") == "LeastPrivilege"
        and value(settings, "MultipleInstancesPolicy") == "IgnoreNew"
        and _normalized_path(value(action, "Command"))
        == _normalized_path(powershell.resolve())
        and value(action, "Arguments") == expected_arguments
        and _normalized_path(value(action, "WorkingDirectory"))
        == _normalized_path(wrapper.resolve().parent)
    )


def _atomic_private_text(path: Path, content: str) -> None:
    ensure_private_directory(path.parent)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    temporary = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        harden_private_file(temporary)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def install_windows_worker_task(
    data_dir: Path,
    *,
    executable: Path,
    powershell: Path,
    user_sid: str,
    environment: Mapping[str, str],
    scheduler: TaskScheduler,
) -> Path:
    service_dir = data_dir.resolve() / "service"
    wrapper = service_dir / "worker.ps1"
    desired_xml = render_worker_task_xml(
        user_sid=user_sid,
        powershell=powershell,
        wrapper=wrapper,
    )
    existing_xml = scheduler.export()
    if existing_xml is not None and not task_matches_worker_identity(
        existing_xml,
        user_sid=user_sid,
        powershell=powershell,
        wrapper=wrapper,
    ):
        raise RuntimeError(
            f"refusing to overwrite foreign Scheduled Task {WINDOWS_WORKER_TASK_URI}"
        )
    if wrapper.exists():
        if not is_managed_worker_wrapper(wrapper.read_text(encoding="utf-8")):
            raise RuntimeError(f"refusing to overwrite foreign worker wrapper: {wrapper}")
    wrapper_existed = wrapper.exists()
    previous_wrapper = wrapper.read_text(encoding="utf-8") if wrapper_existed else ""
    existing_state = scheduler.state() if existing_xml is not None else None
    task_touched = False
    desired_wrapper = render_worker_wrapper(
        executable=executable,
        environment=environment,
    )

    try:
        if existing_xml is not None:
            task_touched = True
            scheduler.stop_and_wait()
        _atomic_private_text(wrapper, desired_wrapper)
        task_touched = True
        scheduler.register(desired_xml)
        scheduler.start()
    except Exception as exc:
        rollback_errors: list[str] = []
        if task_touched:
            try:
                scheduler.stop_and_wait()
            except Exception as rollback_exc:
                rollback_errors.append(f"stop replacement Task: {rollback_exc}")
        if wrapper_existed:
            try:
                _atomic_private_text(wrapper, previous_wrapper)
            except Exception as rollback_exc:
                rollback_errors.append(f"worker wrapper: {rollback_exc}")
        else:
            try:
                wrapper.unlink(missing_ok=True)
            except Exception as rollback_exc:
                rollback_errors.append(f"worker wrapper: {rollback_exc}")
        if task_touched:
            try:
                if existing_xml is None:
                    scheduler.unregister()
                else:
                    scheduler.register(existing_xml)
                    if existing_state in {"Running", "Queued"}:
                        scheduler.start()
            except Exception as rollback_exc:
                rollback_errors.append(f"Scheduled Task: {rollback_exc}")
        if rollback_errors:
            raise RuntimeError(
                f"{exc}; install rollback failed: {'; '.join(rollback_errors)}"
            ) from exc
        raise
    return wrapper


def worker_task_checks(
    *,
    wrapper: Path,
    executable: Path,
    powershell: Path,
    user_sid: str,
    environment: Mapping[str, str],
    scheduler: TaskScheduler,
) -> list[tuple[str, bool, str]]:
    try:
        xml = scheduler.export()
        state = scheduler.state()
    except Exception as exc:
        return [
            (
                "Windows worker Task",
                False,
                f"{WINDOWS_WORKER_TASK_URI}: {type(exc).__name__}",
            )
        ]
    exists = xml is not None
    identity_ok = bool(
        xml
        and task_matches_worker_identity(
            xml,
            user_sid=user_sid,
            powershell=powershell,
            wrapper=wrapper,
        )
    )
    expected_wrapper = render_worker_wrapper(
        executable=executable,
        environment=environment,
    )
    try:
        wrapper_ok = wrapper.read_text(encoding="utf-8") == expected_wrapper
    except (OSError, UnicodeError):
        wrapper_ok = False
    return [
        (
            "Windows worker Task exists",
            exists,
            WINDOWS_WORKER_TASK_URI,
        ),
        (
            "Windows worker Task identity",
            identity_ok,
            "expected current-user InteractiveToken, LeastPrivilege, IgnoreNew, and managed action",
        ),
        (
            "Windows worker wrapper provenance",
            wrapper_ok,
            str(wrapper),
        ),
        (
            "Windows worker Task running",
            state == "Running",
            f"state={state or '<missing>'}",
        ),
    ]


def uninstall_windows_worker_task(
    wrapper: Path,
    *,
    powershell: Path,
    user_sid: str,
    scheduler: TaskScheduler,
) -> Path:
    existing_xml = scheduler.export()
    _validate_managed_wrapper(wrapper)
    if existing_xml is None:
        _remove_managed_wrapper(wrapper)
        return wrapper
    if not task_matches_worker_identity(
        existing_xml,
        user_sid=user_sid,
        powershell=powershell,
        wrapper=wrapper,
    ):
        raise RuntimeError(
            f"refusing to remove foreign Scheduled Task {WINDOWS_WORKER_TASK_URI}"
        )
    existing_state = scheduler.state()
    backup_wrapper: Path | None = None
    try:
        scheduler.stop_and_wait()
        if wrapper.exists():
            backup_dir = (
                wrapper.parent.parent
                / "backups"
                / f"uninstall-{uuid.uuid4().hex}"
            )
            ensure_private_directory(backup_dir)
            backup_wrapper = backup_dir / wrapper.name
            os.replace(wrapper, backup_wrapper)
        scheduler.unregister()
    except Exception as exc:
        rollback_errors: list[str] = []
        if backup_wrapper is not None and backup_wrapper.exists():
            try:
                os.replace(backup_wrapper, wrapper)
            except Exception as rollback_exc:
                rollback_errors.append(f"worker wrapper: {rollback_exc}")
        try:
            scheduler.register(existing_xml)
            if existing_state in {"Running", "Queued"}:
                scheduler.start()
        except Exception as rollback_exc:
            rollback_errors.append(f"Scheduled Task: {rollback_exc}")
        if rollback_errors:
            raise RuntimeError(
                f"{exc}; uninstall rollback failed: {'; '.join(rollback_errors)}"
            ) from exc
        raise
    return wrapper


def _remove_managed_wrapper(wrapper: Path) -> None:
    _validate_managed_wrapper(wrapper)
    if not wrapper.exists():
        return
    backup_dir = (
        wrapper.parent.parent
        / "backups"
        / f"uninstall-{uuid.uuid4().hex}"
    )
    ensure_private_directory(backup_dir)
    os.replace(wrapper, backup_dir / wrapper.name)


def _validate_managed_wrapper(wrapper: Path) -> None:
    if wrapper.exists():
        if not is_managed_worker_wrapper(wrapper.read_text(encoding="utf-8")):
            raise RuntimeError(f"refusing to remove foreign worker wrapper: {wrapper}")
