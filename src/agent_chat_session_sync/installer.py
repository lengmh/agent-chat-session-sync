from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import shutil
import sys
import tempfile
import plistlib
import subprocess
from typing import Any


DESCRIPTION = "Mirror local Codex and Claude Code sessions to dedicated cc-connect chat groups."
EVENTS: dict[str, dict[str, Any]] = {
    "SessionStart": {"matcher": "^startup$", "statusMessage": "正在登记飞书同步事件"},
    "UserPromptSubmit": {},
    "Stop": {},
}


def default_hooks_path() -> Path:
    return Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "hooks.json"


def installed_executable() -> str | None:
    # Keep the venv path itself: its Python is commonly a symlink to a global
    # interpreter, while the console-script entry point lives beside the link.
    adjacent = Path(sys.executable).parent / "agent-chat-session-sync"
    if adjacent.is_file() and os.access(adjacent, os.X_OK):
        return str(adjacent.resolve())
    return shutil.which("agent-chat-session-sync")


def hook_command(agent_type: str = "codex") -> str:
    executable = installed_executable()
    if executable:
        parts = [executable, "hook", "--agent", agent_type]
    else:
        parts = [sys.executable, "-m", "agent_chat_session_sync", "hook", "--agent", agent_type]
    return subprocess_command(parts)


def subprocess_command(parts: list[str]) -> str:
    if os.name == "nt":
        import subprocess

        return subprocess.list2cmdline(parts)
    return shlex.join(parts)


def _ours(entry: dict[str, Any]) -> bool:
    for hook in entry.get("hooks", []):
        command = str(hook.get("command", ""))
        if (
            "agent-chat-session-sync" in command
            or "agent_chat_session_sync" in command
            or "codex_lark_sync.py" in command
        ):
            return True
    return False


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def _install_hooks(path: Path, command: str) -> Path:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        document = {}
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"refusing to overwrite invalid JSON in {path}: {exc}") from exc
    hooks = document.get("hooks")
    if not isinstance(hooks, dict):
        hooks = {}
        document["hooks"] = hooks
    for event, options in EVENTS.items():
        entries = [entry for entry in hooks.get(event, []) if not _ours(entry)]
        inner: dict[str, Any] = {"type": "command", "command": command, "timeout": 10}
        if options.get("statusMessage"):
            inner["statusMessage"] = options["statusMessage"]
        entry: dict[str, Any] = {"hooks": [inner]}
        if options.get("matcher"):
            entry["matcher"] = options["matcher"]
        entries.append(entry)
        hooks[event] = entries
    document["description"] = document.get("description") or DESCRIPTION
    _atomic_json(path, document)
    return path


def install_codex_hooks(path: Path | None = None, command: str | None = None) -> Path:
    return _install_hooks(path or default_hooks_path(), command or hook_command("codex"))


def default_claude_settings_path() -> Path:
    return Path(os.environ.get("CLAUDE_HOME", Path.home() / ".claude")) / "settings.json"


def install_claude_hooks(path: Path | None = None, command: str | None = None) -> Path:
    return _install_hooks(path or default_claude_settings_path(), command or hook_command("claudecode"))


def _uninstall_hooks(path: Path) -> Path:
    document = json.loads(path.read_text(encoding="utf-8"))
    hooks = document.get("hooks", {})
    for event in EVENTS:
        entries = [entry for entry in hooks.get(event, []) if not _ours(entry)]
        if entries:
            hooks[event] = entries
        else:
            hooks.pop(event, None)
    _atomic_json(path, document)
    return path


def uninstall_codex_hooks(path: Path | None = None) -> Path:
    return _uninstall_hooks(path or default_hooks_path())


def uninstall_claude_hooks(path: Path | None = None) -> Path:
    return _uninstall_hooks(path or default_claude_settings_path())


WORKER_LABEL = "com.agent-chat-session-sync.worker"


def worker_plist_path() -> Path:
    return Path.home() / "Library/LaunchAgents" / f"{WORKER_LABEL}.plist"


def install_worker_service(data_dir: Path, executable: str | None = None) -> Path:
    if sys.platform != "darwin":
        raise RuntimeError("automatic worker service installation currently supports macOS LaunchAgent")
    executable = executable or installed_executable()
    if not executable:
        raise RuntimeError("agent-chat-session-sync executable not found")
    path = worker_plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)
    document = {
        "Label": WORKER_LABEL,
        "ProgramArguments": [str(Path(executable).resolve()), "worker"],
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Background",
        "StandardOutPath": str(data_dir / "worker.log"),
        "StandardErrorPath": str(data_dir / "worker.log"),
        "EnvironmentVariables": {
            "ACSS_DATA_DIR": str(data_dir),
            "CODEX_HOME": os.environ.get("CODEX_HOME", str(Path.home() / ".codex")),
            "CLAUDE_HOME": os.environ.get("CLAUDE_HOME", str(Path.home() / ".claude")),
            "CC_CONNECT_CONFIG": os.environ.get(
                "CC_CONNECT_CONFIG", str(Path.home() / ".cc-connect/config.toml")
            ),
            "CC_CONNECT_SOCKET": os.environ.get(
                "CC_CONNECT_SOCKET", str(Path.home() / ".cc-connect/run/api.sock")
            ),
        },
    }
    payload = plistlib.dumps(document, fmt=plistlib.FMT_XML, sort_keys=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", domain, str(path)], check=False, capture_output=True)
    subprocess.run(["launchctl", "bootstrap", domain, str(path)], check=True)
    subprocess.run(["launchctl", "kickstart", "-k", f"{domain}/{WORKER_LABEL}"], check=True)
    return path


def uninstall_worker_service() -> Path:
    path = worker_plist_path()
    if sys.platform == "darwin" and path.exists():
        subprocess.run(
            ["launchctl", "bootout", f"gui/{os.getuid()}", str(path)], check=False, capture_output=True
        )
    path.unlink(missing_ok=True)
    return path
