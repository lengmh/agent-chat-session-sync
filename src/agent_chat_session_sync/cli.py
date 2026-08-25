from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

from . import __version__
from .acceptance import LiveAcceptance
from .bridges.cc_connect import CCConnectBridge
from .cc_configurator import configure_claude_project, rename_agent_projects
from .config import Settings, load_cc_connect_config
from .installer import (
    install_codex_hooks,
    install_claude_hooks,
    installed_executable,
    install_worker_service,
    uninstall_codex_hooks,
    uninstall_claude_hooks,
    uninstall_worker_service,
)
from .locking import LockUnavailableError, exclusive_file_lock
from .models import Binding
from .permissions import (
    codex_permission_config_checks,
    local_endpoint_security_checks,
    private_path_security_checks,
    socket_security_checks,
)
from .runtime import hook_main, make_logger
from .provenance import current_provenance, provenance_json, source_head
from .queue import EventDatabase
from .security import current_windows_user_sid_string
from .windows_tasks import (
    PowerShellTaskScheduler,
    windows_worker_environment,
    worker_task_checks,
)
from .worker import EventWorker
from .windows_configurator import (
    apply_windows_configuration,
    plan_codex_permission_profile,
    plan_windows_configuration,
)


class _ConfigureWindowsMode(argparse.Action):
    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: object,
        option_string: str | None = None,
    ) -> None:
        apply = option_string == "--apply"
        namespace.apply = apply
        namespace.check = not apply


def _windows_worker_task_checks(
    settings: Settings,
) -> list[tuple[str, bool, str]]:
    executable = installed_executable()
    if not executable:
        return [
            (
                "Windows worker package provenance",
                False,
                "agent-chat-session-sync executable not found",
            )
        ]
    powershell_value = shutil.which("pwsh.exe") or shutil.which("pwsh")
    if not powershell_value:
        return [
            (
                "Windows worker PowerShell",
                False,
                "PowerShell 7 executable not found",
            )
        ]
    try:
        user_sid = current_windows_user_sid_string()
        powershell = Path(powershell_value).resolve()
        wrapper = settings.data_dir / "service" / "worker.ps1"
        expected_provenance = current_provenance().to_dict()
        probe = subprocess.run(
            [executable, "provenance", "--json"],
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=False,
        )
        try:
            actual_provenance = json.loads(probe.stdout) if probe.returncode == 0 else {}
        except json.JSONDecodeError:
            actual_provenance = {}
        identity_fields = ("git_commit", "package_path", "python_path")
        provenance_ok = probe.returncode == 0 and all(
            actual_provenance.get(field) == expected_provenance.get(field)
            for field in identity_fields
        )
        checks = [
            (
                "Windows worker package provenance",
                provenance_ok,
                (
                    "matches current package"
                    if provenance_ok
                    else "worker executable provenance does not match current package"
                ),
            )
        ]
        checks.extend(worker_task_checks(
            wrapper=wrapper,
            executable=Path(executable),
            powershell=powershell,
            user_sid=user_sid,
            environment=windows_worker_environment(
                settings.data_dir,
                user_sid=user_sid,
            ),
            scheduler=PowerShellTaskScheduler(powershell),
        ))
        return checks
    except Exception as exc:
        return [
            (
                "Windows worker Task",
                False,
                f"{type(exc).__name__}",
            )
        ]


def _doctor(settings: Settings) -> int:
    checks = [
        ("cc-connect config", settings.cc_config.is_file(), str(settings.cc_config)),
        ("Codex sessions", (settings.codex_home / "sessions").is_dir(), str(settings.codex_home / "sessions")),
        ("Claude Code CLI", shutil.which("claude") is not None, shutil.which("claude") or "claude not found"),
        ("Claude sessions", (settings.claude_home / "projects").is_dir(), str(settings.claude_home / "projects")),
        ("Codex Hook", (settings.codex_home / "hooks.json").is_file(), str(settings.codex_home / "hooks.json")),
        ("Claude Hook", (settings.claude_home / "settings.json").is_file(), str(settings.claude_home / "settings.json")),
    ]
    for check in private_path_security_checks("data directory", settings.data_dir):
        checks.append((check.name, check.okay, check.detail))
    private_runtime_paths = (
        ("SQLite database", settings.database_path),
        ("SQLite WAL", Path(f"{settings.database_path}-wal")),
        ("SQLite shared memory", Path(f"{settings.database_path}-shm")),
        ("emergency spool", settings.data_dir / "emergency-inbox.jsonl"),
        ("runtime log", settings.log_path),
        ("worker log", settings.worker_log_path),
        ("worker lock", settings.lock_path),
    )
    for name, path in private_runtime_paths:
        if not path.exists():
            continue
        for check in private_path_security_checks(name, path):
            checks.append((check.name, check.okay, check.detail))
    try:
        config = load_cc_connect_config(settings.cc_config)
        projects = config.get("projects", [])
        configured = any(
            any(platform.get("type") == "feishu" for platform in project.get("platforms", [])) for project in projects
        )
        agent_types = {
            str(project.get("agent", {}).get("type", "codex")).lower()
            for project in projects
            if any(platform.get("type") == "feishu" for platform in project.get("platforms", []))
        }
        shared_apps: dict[str, list[dict]] = {}
        for project in projects:
            for platform in project.get("platforms", []):
                if platform.get("type") != "feishu":
                    continue
                app_id = str(platform.get("options", {}).get("app_id", ""))
                if app_id:
                    shared_apps.setdefault(app_id, []).append(platform)
        routing_ok = all(
            len(platforms) == 1
            or all(bool(platform.get("options", {}).get("binding_routing")) for platform in platforms)
            for platforms in shared_apps.values()
        )
    except Exception:
        configured = False
        agent_types = set()
        routing_ok = False
    checks.append(("Feishu project", configured, "at least one cc-connect project"))
    checks.append(("Codex project", "codex" in agent_types, "Codex + Feishu"))
    checks.append(("Claude project", "claudecode" in agent_types, "Claude Code + Feishu"))
    checks.append(("shared Bot routing", routing_ok, "binding_routing=true for projects sharing app_id"))
    bridge = CCConnectBridge(settings.local_endpoint)
    try:
        bridge_info = bridge.inspect()
        capabilities = set(bridge_info.capabilities)
        endpoint_ok = True
        endpoint_detail = (
            f"{settings.local_endpoint} transport={bridge_info.transport} "
            f"instance_id={bridge_info.instance_id}"
        )
    except Exception as exc:
        bridge_info = None
        capabilities = set()
        endpoint_ok = False
        endpoint_detail = f"{settings.local_endpoint}: {exc}"
    checks.append(("cc-connect endpoint", endpoint_ok, endpoint_detail))
    checks.append(
        (
            "bind-agent extension",
            "attach_agent_session" in capabilities,
            "GET /sessions/bind-agent",
        )
    )
    checks.append(("binding routing extension", "binding_routing" in capabilities, "GET /sessions/bind-agent"))
    checks.append(
        (
            "external rollout refresh",
            "external_session_refresh" in capabilities,
            "recycle stale Codex process before platform message",
        )
    )
    checks.append(
        (
            "local endpoint v2",
            "local_endpoint_v2" in capabilities,
            "transport and instance_id discovery",
        )
    )
    try:
        EventDatabase(settings.database_path)
        database_ok = True
        database_detail = str(settings.database_path)
    except Exception as exc:
        database_ok = False
        database_detail = str(exc)
    checks.append(("durable SQLite queue", database_ok, database_detail))
    provenance = current_provenance()
    checks.append(
        ("stamped package", provenance.git_commit not in {"UNSTAMPED", "UNKNOWN", ""}, provenance.git_commit)
    )
    if sys.platform == "darwin":
        service = subprocess.run(
            ["launchctl", "print", f"gui/{os.getuid()}/com.agent-chat-session-sync.worker"],
            text=True,
            capture_output=True,
        )
        checks.append(("durable worker service", service.returncode == 0, "LaunchAgent running"))
    elif os.name == "nt":
        checks.extend(_windows_worker_task_checks(settings))
        for name, path in (
            ("worker service directory", settings.data_dir / "service"),
            ("worker service wrapper", settings.data_dir / "service" / "worker.ps1"),
        ):
            if path.exists():
                for check in private_path_security_checks(name, path):
                    checks.append((check.name, check.okay, check.detail))
    for check in local_endpoint_security_checks(
        "cc-connect endpoint",
        settings.local_endpoint,
    ):
        checks.append((check.name, check.okay, check.detail))
    if configured:
        for check in codex_permission_config_checks(config):
            checks.append((check.name, check.okay, check.detail))
        daemon_projects = [
            project
            for project in config.get("projects", [])
            if str(project.get("agent", {}).get("options", {}).get("app_server_lifecycle", "")).lower()
            in {"daemon", "shared", "persistent"}
        ]
        if daemon_projects:
            configured_socket = next(
                (
                    Path(project.get("agent", {}).get("options", {}).get("app_server_socket", "")).expanduser()
                    for project in daemon_projects
                    if project.get("agent", {}).get("options", {}).get("app_server_socket")
                ),
                settings.codex_app_server_socket,
            )
            for check in socket_security_checks("Codex App Server socket", configured_socket):
                checks.append((check.name, check.okay, check.detail))
    for name, okay, detail in checks:
        print(f"{'OK' if okay else 'FAIL':4}  {name}: {detail}")
    return 0 if all(okay for _, okay, _ in checks) else 1


def _status(settings: Settings) -> int:
    database = EventDatabase(settings.database_path)
    stats = database.stats()
    print(f"database: {settings.database_path}")
    for key in sorted(stats):
        print(f"{key}: {stats[key]}")
    return 0


def _configure_windows(settings: Settings, *, apply: bool) -> int:
    try:
        plan = plan_windows_configuration(
            settings.cc_config,
            settings.local_endpoint,
        )
        codex_plan = plan_codex_permission_profile(
            settings.codex_home / "config.toml",
        )
    except (OSError, RuntimeError, UnicodeError, ValueError) as exc:
        print(
            f"CONFLICT   cc-connect config: {settings.cc_config}; "
            f"{type(exc).__name__}",
        )
        return 1
    for line in plan.report_lines():
        print(line)
    for line in codex_plan.report_lines():
        print(line)
    if plan.has_conflicts or codex_plan.has_conflicts:
        return 1
    if not apply:
        return 0
    result = apply_windows_configuration(
        plan,
        settings.data_dir,
        additional_plans=(codex_plan,),
    )
    if result.changed:
        print(f"APPLIED    cc-connect config; backup={result.backup_dir}")
    else:
        print("CONSISTENT cc-connect config; no changes required")
    return 0


def _events(settings: Settings, limit: int) -> int:
    for event in EventDatabase(settings.database_path).list_events(limit):
        print(
            f"{event.id:6} {event.state:20} attempts={event.attempts:<3} "
            f"rollout={event.rollout_id or '-'} method={event.resolution_method or '-'} "
            f"error={event.last_error[:120] or '-'}"
        )
        if event.candidates:
            print("       candidates=" + json.dumps(event.candidates, ensure_ascii=False))
    return 0


def _resolve_event(settings: Settings, event_id: int, rollout_id: str, rollout_path: Path | None) -> int:
    path = rollout_path
    if path is None:
        try:
            path = next((settings.codex_home / "sessions").glob(f"**/*{rollout_id}.jsonl"))
        except StopIteration:
            print(f"rollout not found: {rollout_id}", file=sys.stderr)
            return 1
    EventDatabase(settings.database_path).force_resolution(event_id, rollout_id.lower(), str(path.resolve()))
    print(f"resolved inbox={event_id} rollout={rollout_id} path={path}")
    return 0


def _hook_provenance(path: Path) -> dict[str, str]:
    document = json.loads(path.read_text(encoding="utf-8"))
    command = ""
    for entries in document.get("hooks", {}).values():
        for entry in entries:
            for hook in entry.get("hooks", []):
                candidate = str(hook.get("command", ""))
                if "agent-chat-session-sync" in candidate or "agent_chat_session_sync" in candidate:
                    command = candidate
                    break
            if command:
                break
        if command:
            break
    if not command:
        raise RuntimeError(f"installed Hook command not found in {path}")
    parts = _split_command(command)
    if "-m" in parts and "agent_chat_session_sync" in parts:
        probe = parts[: parts.index("agent_chat_session_sync") + 1] + ["provenance", "--json"]
    else:
        probe = [parts[0], "provenance", "--json"]
    result = subprocess.run(probe, check=True, text=True, capture_output=True)
    return json.loads(result.stdout)


def _split_command(command: str) -> list[str]:
    if os.name != "nt":
        return shlex.split(command)

    import ctypes
    from ctypes import wintypes

    argc = ctypes.c_int()
    command_line_to_argv = ctypes.WinDLL(
        "shell32", use_last_error=True
    ).CommandLineToArgvW
    command_line_to_argv.argtypes = [
        wintypes.LPCWSTR,
        ctypes.POINTER(ctypes.c_int),
    ]
    command_line_to_argv.restype = ctypes.POINTER(wintypes.LPWSTR)
    argv = command_line_to_argv(command, ctypes.byref(argc))
    if not argv:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return [argv[index] for index in range(argc.value)]
    finally:
        local_free = ctypes.WinDLL("kernel32", use_last_error=True).LocalFree
        local_free.argtypes = [wintypes.HLOCAL]
        local_free.restype = wintypes.HLOCAL
        local_free(argv)


def _verify_install(
    source: Path,
    expected_commit: str,
    hooks_file: Path | None = None,
    claude_settings_file: Path | None = None,
) -> int:
    source = source.resolve()
    head = source_head(source)
    dirty = subprocess.run(
        ["git", "status", "--porcelain"], cwd=source, text=True, capture_output=True, check=True
    ).stdout.strip()
    installed = current_provenance().to_dict()
    hook = _hook_provenance(hooks_file or (Path.home() / ".codex/hooks.json"))
    claude_hook = _hook_provenance(
        claude_settings_file or (Path.home() / ".claude/settings.json")
    )
    expected = expected_commit or head
    checks = provenance_checks(head, dirty, expected, installed, hook, claude_hook)
    for name, okay, detail in checks:
        print(f"{'OK' if okay else 'FAIL':4}  {name}: {detail}")
    return 0 if all(check[1] for check in checks) else 1


def provenance_checks(
    head: str,
    dirty: str,
    expected: str,
    installed: dict[str, str],
    hook: dict[str, str],
    claude_hook: dict[str, str] | None = None,
) -> list[tuple[str, bool, str]]:
    checks = [
        ("source repository clean", not dirty, "clean" if not dirty else "working tree has uncommitted changes"),
        ("source commit", head == expected, f"source={head} expected={expected}"),
        ("built package commit", installed["git_commit"] == expected, f"package={installed['git_commit']}"),
        ("Hook imported commit", hook.get("git_commit") == expected, f"hook={hook.get('git_commit')}"),
        (
            "Hook package path",
            hook.get("package_path") == installed["package_path"],
            f"hook={hook.get('package_path')} verifier={installed['package_path']}",
        ),
        (
            "Hook Python path",
            hook.get("python_path") == installed["python_path"],
            f"hook={hook.get('python_path')} verifier={installed['python_path']}",
        ),
    ]
    if claude_hook is not None:
        checks.extend(
            [
                (
                    "Claude Hook imported commit",
                    claude_hook.get("git_commit") == expected,
                    f"claude_hook={claude_hook.get('git_commit')}",
                ),
                (
                    "Claude Hook package path",
                    claude_hook.get("package_path") == installed["package_path"],
                    f"claude_hook={claude_hook.get('package_path')} verifier={installed['package_path']}",
                ),
                (
                    "Claude Hook Python path",
                    claude_hook.get("python_path") == installed["python_path"],
                    f"claude_hook={claude_hook.get('python_path')} verifier={installed['python_path']}",
                ),
            ]
        )
    return checks


def _migrate_state(settings: Settings, source: Path) -> int:
    source_state = json.loads(source.expanduser().read_text(encoding="utf-8"))
    config = load_cc_connect_config(settings.cc_config)
    bridge = CCConnectBridge(settings.local_endpoint)
    migrated = 0
    skipped = 0
    from .config import matching_project

    database = EventDatabase(settings.database_path)
    for session_id, raw in source_state.get("sessions", {}).items():
        binding = Binding.from_dict(raw)
        project = matching_project(config, binding.cwd)
        if project is None or not binding.chat_id or not binding.session_key or not Path(binding.cwd).is_dir():
            skipped += 1
            continue
        name = f"Codex · {Path(binding.cwd).name} · {session_id[:8]}"
        bridge.attach_agent_session(project.name, binding.session_key, session_id, name, binding.cwd)
        database.put_binding(
            session_id,
            Binding(
                chat_id=binding.chat_id,
                session_key=binding.session_key,
                project=project.name,
                cwd=binding.cwd,
                generation=binding.generation,
                created_at=binding.created_at,
                title=binding.title,
            ),
        )
        migrated += 1
    print(f"migrated: {migrated}; skipped: {skipped}; destination: {settings.database_path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-chat-session-sync")
    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    hook = subparsers.add_parser("hook", help="consume one Agent hook event from stdin")
    hook.add_argument("--agent", choices=("codex", "claudecode"), default="codex")
    worker = subparsers.add_parser("worker", help="process the durable event queue")
    worker.add_argument("--once", action="store_true")
    worker.add_argument("--poll-interval", type=float, default=1.0)
    install = subparsers.add_parser("install-hooks", help="install or update Codex hooks")
    install.add_argument("--hooks-file", type=Path)
    install.add_argument("--claude-settings-file", type=Path)
    uninstall = subparsers.add_parser("uninstall-hooks", help="remove this project's Codex hooks")
    uninstall.add_argument("--hooks-file", type=Path)
    uninstall.add_argument("--claude-settings-file", type=Path)
    subparsers.add_parser("install-service", help="install and start the durable worker service")
    subparsers.add_parser("uninstall-service", help="stop and remove the durable worker service")
    subparsers.add_parser("doctor", help="check local prerequisites and the cc-connect extension")
    subparsers.add_parser("status", help="show local bindings without exposing chat or user IDs")
    events = subparsers.add_parser("events", help="show recent durable inbox states")
    events.add_argument("--limit", type=int, default=50)
    resolve = subparsers.add_parser("resolve", help="manually confirm an ambiguous inbox event")
    resolve.add_argument("event_id", type=int)
    resolve.add_argument("rollout_id")
    resolve.add_argument("--rollout-path", type=Path)
    retry = subparsers.add_parser("retry", help="retry a non-delivered historical event immediately")
    retry.add_argument("event_id", type=int)
    provenance = subparsers.add_parser("provenance", help="show the imported package build identity")
    provenance.add_argument("--json", action="store_true")
    verify = subparsers.add_parser("verify-install", help="prove source, build and Hook identities match")
    verify.add_argument("--source", type=Path, required=True)
    verify.add_argument("--expected-commit", default="")
    verify.add_argument("--hooks-file", type=Path)
    verify.add_argument("--claude-settings-file", type=Path)
    acceptance = subparsers.add_parser("acceptance-live", help="run the real Agent/Hook/Feishu acceptance flow")
    acceptance.add_argument("--agent", choices=("codex", "claudecode"), default="codex")
    acceptance.add_argument("--timeout", type=float, default=300)
    acceptance.add_argument("--keep-resources", action="store_true")
    acceptance.add_argument(
        "--skip-reply",
        action="store_true",
        help="diagnostic only: do not verify Feishu reply resumes the same rollout",
    )
    migrate = subparsers.add_parser("migrate-state", help="reattach bindings from a legacy state file")
    migrate.add_argument("--from", dest="source", type=Path, required=True)
    configure_claude = subparsers.add_parser(
        "configure-claude", help="clone Feishu settings into a routed Claude Code project"
    )
    configure_claude.add_argument("--project-name", default="")
    configure_windows = subparsers.add_parser(
        "configure-windows",
        help="check or safely apply the Windows cc-connect configuration profile",
    )
    configure_windows.set_defaults(check=True, apply=False)
    configure_mode = configure_windows.add_mutually_exclusive_group()
    configure_mode.add_argument(
        "--check",
        nargs=0,
        action=_ConfigureWindowsMode,
        help="show redacted configuration checks without writing files (default)",
    )
    configure_mode.add_argument(
        "--apply",
        nargs=0,
        action=_ConfigureWindowsMode,
        help="back up and apply only non-conflicting Windows configuration changes",
    )
    rename_projects = subparsers.add_parser(
        "rename-projects", help="rename Feishu Agent engines and migrate durable bindings"
    )
    rename_projects.add_argument("--codex-name", default="local-codex")
    rename_projects.add_argument("--claude-name", default="local-claude")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "hook":
        return hook_main(args.agent)
    if args.command == "provenance":
        if args.json:
            print(provenance_json())
        else:
            for key, value in current_provenance().to_dict().items():
                print(f"{key}: {value}")
        return 0
    if args.command == "install-hooks":
        print(f"installed Codex hooks: {install_codex_hooks(args.hooks_file)}")
        print(f"installed Claude Code hooks: {install_claude_hooks(args.claude_settings_file)}")
        return 0
    if args.command == "uninstall-hooks":
        print(f"removed Codex hooks: {uninstall_codex_hooks(args.hooks_file)}")
        print(f"removed Claude Code hooks: {uninstall_claude_hooks(args.claude_settings_file)}")
        return 0
    settings = Settings.from_env()
    if args.command == "worker":
        try:
            with exclusive_file_lock(settings.lock_path, blocking=False):
                worker = EventWorker(settings, make_logger(settings.worker_log_path))
                if args.once:
                    return 0 if worker.run_once() else 3
                worker.run_forever(args.poll_interval)
                return 0
        except LockUnavailableError as exc:
            print(f"worker not started: {exc}", file=sys.stderr)
            return 4
    if args.command == "install-service":
        print(f"installed worker: {install_worker_service(settings.data_dir)}")
        return 0
    if args.command == "uninstall-service":
        print(f"removed worker: {uninstall_worker_service(settings.data_dir)}")
        return 0
    if args.command == "doctor":
        return _doctor(settings)
    if args.command == "status":
        return _status(settings)
    if args.command == "events":
        return _events(settings, args.limit)
    if args.command == "resolve":
        return _resolve_event(settings, args.event_id, args.rollout_id, args.rollout_path)
    if args.command == "retry":
        okay = EventDatabase(settings.database_path).retry_now(args.event_id)
        print(f"{'scheduled' if okay else 'not scheduled'} inbox={args.event_id}")
        return 0 if okay else 1
    if args.command == "verify-install":
        return _verify_install(
            args.source,
            args.expected_commit,
            args.hooks_file,
            args.claude_settings_file,
        )
    if args.command == "acceptance-live":
        result = LiveAcceptance(settings, make_logger(settings.worker_log_path)).run(
            timeout=args.timeout,
            keep_resources=args.keep_resources,
            skip_reply=args.skip_reply,
            agent_type=args.agent,
        )
        print(json.dumps(result.__dict__, ensure_ascii=False, sort_keys=True))
        if args.skip_reply:
            print("DIAGNOSTIC ONLY: Feishu → same rollout was not verified.", file=sys.stderr)
        return 0
    if args.command == "migrate-state":
        return _migrate_state(settings, args.source)
    if args.command == "configure-claude":
        backup, name, created = configure_claude_project(settings.cc_config, args.project_name)
        print(f"{'created' if created else 'updated'} Claude project: {name}; backup: {backup}")
        return 0
    if args.command == "configure-windows":
        if os.name != "nt":
            print("configure-windows requires Windows", file=sys.stderr)
            return 2
        return _configure_windows(settings, apply=args.apply)
    if args.command == "rename-projects":
        backup, renamed = rename_agent_projects(
            settings.cc_config, args.codex_name, args.claude_name
        )
        database = EventDatabase(settings.database_path)
        migrated = {
            old: database.rename_binding_project(old, new) for old, new in renamed.items()
        }
        print(f"renamed projects: {renamed}; migrated bindings: {migrated}; backup: {backup}")
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
