from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import tomllib
from typing import Any

from .endpoints import LocalEndpoint
from .models import Project


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    cc_config: Path
    cc_socket: Path
    codex_home: Path
    claude_home: Path = field(default_factory=lambda: Path.home() / ".claude")
    cc_endpoint: LocalEndpoint | None = None

    @property
    def local_endpoint(self) -> LocalEndpoint:
        if self.cc_endpoint is not None:
            return self.cc_endpoint
        return LocalEndpoint("unix", str(self.cc_socket))

    @property
    def codex_app_server_socket(self) -> Path:
        return Path(
            os.environ.get(
                "CODEX_APP_SERVER_SOCKET",
                self.codex_home / "app-server-control/app-server-control.sock",
            )
        ).expanduser()

    @classmethod
    def from_env(cls) -> "Settings":
        home = Path.home()
        if os.name == "nt":
            local_app_data = Path(os.environ.get("LOCALAPPDATA", home / "AppData/Local"))
            default_data_dir = local_app_data / "agent-chat-session-sync"
        else:
            default_data_dir = home / ".local/share/agent-chat-session-sync"
        data_dir = Path(os.environ.get("ACSS_DATA_DIR", default_data_dir))
        endpoint_value = os.environ.get("CC_CONNECT_ENDPOINT")
        return cls(
            data_dir=data_dir.expanduser(),
            cc_config=Path(os.environ.get("CC_CONNECT_CONFIG", home / ".cc-connect/config.toml")).expanduser(),
            cc_socket=Path(os.environ.get("CC_CONNECT_SOCKET", home / ".cc-connect/run/api.sock")).expanduser(),
            codex_home=Path(os.environ.get("CODEX_HOME", home / ".codex")).expanduser(),
            claude_home=Path(os.environ.get("CLAUDE_HOME", home / ".claude")).expanduser(),
            cc_endpoint=LocalEndpoint.parse(endpoint_value) if endpoint_value else None,
        )

    @property
    def state_path(self) -> Path:
        return self.data_dir / "state.json"

    @property
    def database_path(self) -> Path:
        return self.data_dir / "events.sqlite3"

    @property
    def lock_path(self) -> Path:
        return self.data_dir / "state.lock"

    @property
    def log_path(self) -> Path:
        return self.data_dir / "sync.log"

    @property
    def worker_log_path(self) -> Path:
        return self.data_dir / "worker.log"


def load_cc_connect_config(path: Path) -> dict[str, Any]:
    return tomllib.loads(path.read_text(encoding="utf-8"))


def matching_project(config: dict[str, Any], cwd: str, agent_type: str = "") -> Project | None:
    try:
        current = Path(cwd).resolve()
    except OSError:
        return None
    candidates: list[tuple[int, Project]] = []
    for raw in config.get("projects", []):
        agent = raw.get("agent", {})
        configured_agent_type = str(agent.get("type", "codex")).lower()
        if agent_type and configured_agent_type != agent_type.lower():
            continue
        work_dir = agent.get("options", {}).get("work_dir")
        mode = str(raw.get("mode", ""))
        base_dir = str(raw.get("base_dir", ""))
        coverage_dir = base_dir if mode == "multi-workspace" and base_dir else work_dir
        if not coverage_dir:
            continue
        try:
            root = Path(coverage_dir).expanduser().resolve()
            current.relative_to(root)
        except (OSError, ValueError):
            continue
        platform = next((p for p in raw.get("platforms", []) if p.get("type") == "feishu"), None)
        if platform is None:
            continue
        project = Project(
            name=str(raw.get("name", "")),
            work_dir=str(Path(work_dir).expanduser().resolve()) if work_dir else str(root),
            mode=mode,
            base_dir=str(root) if mode == "multi-workspace" else "",
            agent_type=configured_agent_type,
            platform_type="feishu",
            platform_options=dict(platform.get("options", {})),
        )
        candidates.append((len(root.parts), project))
    return max(candidates, key=lambda item: item[0])[1] if candidates else None
