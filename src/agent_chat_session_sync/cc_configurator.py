from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import tomllib
from typing import Any


FEISHU_OPTIONS_RE = re.compile(
    r"(?P<head>^\[\[projects\.platforms\]\]\s*$.*?^\s*type\s*=\s*[\"']feishu[\"']\s*$.*?"
    r"^\[projects\.platforms\.options\]\s*$)(?P<body>.*?)(?=^\[|\Z)",
    re.MULTILINE | re.DOTALL,
)
PROJECT_BLOCK_RE = re.compile(r"^\[\[projects\]\].*?(?=^\[\[projects\]\]|\Z)", re.MULTILINE | re.DOTALL)


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    return json.dumps(str(value), ensure_ascii=False)


def _enable_binding_routing(text: str) -> str:
    def replace(match: re.Match[str]) -> str:
        body = match.group("body")
        pattern = re.compile(r"(?m)^\s*binding_routing\s*=.*$")
        if pattern.search(body):
            body = pattern.sub("binding_routing = true", body)
        else:
            body = body.rstrip() + "\nbinding_routing = true\n\n"
        return match.group("head") + body

    return FEISHU_OPTIONS_RE.sub(replace, text)


def _atomic_replace(path: Path, updated: str, backup_suffix: str) -> Path:
    tomllib.loads(updated)
    backup = path.with_suffix(path.suffix + backup_suffix)
    if not backup.exists():
        shutil.copy2(path, backup)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(updated)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, path.stat().st_mode & 0o777)
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
    return backup


def configure_claude_project(path: Path, project_name: str = "") -> tuple[Path, str, bool]:
    """Atomically enable shared-Bot routing and add one Claude Code project."""
    original = path.read_text(encoding="utf-8")
    config = tomllib.loads(original)
    projects = list(config.get("projects", []))
    existing = next(
        (project for project in projects if str(project.get("agent", {}).get("type", "")).lower() == "claudecode"),
        None,
    )
    source = next(
        (
            project
            for project in projects
            if str(project.get("agent", {}).get("type", "codex")).lower() == "codex"
            and any(platform.get("type") == "feishu" for platform in project.get("platforms", []))
        ),
        None,
    )
    if source is None:
        raise RuntimeError("no Codex + Feishu project is available to clone")

    updated = _enable_binding_routing(original)
    created = existing is None
    name = str(existing.get("name", "")) if existing else (project_name or "local-claude")
    if created:
        platform = next(item for item in source["platforms"] if item.get("type") == "feishu")
        options = dict(platform.get("options", {}))
        options["group_reply_all"] = True
        options["binding_routing"] = True
        mode = str(source.get("mode") or "multi-workspace")
        base_dir = str(source.get("base_dir") or "/")
        lines = [
            "", "[[projects]]", f"name = {_toml_value(name)}", f"mode = {_toml_value(mode)}",
            f"base_dir = {_toml_value(base_dir)}", "workspace_init_allow_local_paths = true", "",
            "[projects.agent]", 'type = "claudecode"', "", "[projects.agent.options]", 'mode = "auto"', "",
            "[[projects.platforms]]", 'type = "feishu"', "", "[projects.platforms.options]",
        ]
        lines.extend(f"{key} = {_toml_value(value)}" for key, value in options.items())
        updated = updated.rstrip() + "\n" + "\n".join(lines) + "\n"

    backup = _atomic_replace(path, updated, ".pre-claude.bak")
    return backup, name, created


def rename_agent_projects(
    path: Path,
    codex_name: str = "local-codex",
    claude_name: str = "local-claude",
) -> tuple[Path, dict[str, str]]:
    """Rename Feishu-backed Agent engines without rewriting credentials."""
    original = path.read_text(encoding="utf-8")
    desired = {"codex": codex_name, "claudecode": claude_name}
    renamed: dict[str, str] = {}

    def replace(match: re.Match[str]) -> str:
        block = match.group(0)
        parsed = tomllib.loads(block)["projects"][0]
        agent_type = str(parsed.get("agent", {}).get("type", "codex")).lower()
        if agent_type not in desired or not any(
            platform.get("type") == "feishu" for platform in parsed.get("platforms", [])
        ):
            return block
        old_name = str(parsed.get("name", ""))
        new_name = desired[agent_type]
        if not old_name or old_name == new_name:
            return block
        renamed[old_name] = new_name
        return re.sub(
            r"(?m)^\s*name\s*=.*$",
            f"name = {_toml_value(new_name)}",
            block,
            count=1,
        )

    updated = PROJECT_BLOCK_RE.sub(replace, original)
    names = [str(item.get("name", "")) for item in tomllib.loads(updated).get("projects", [])]
    if len(names) != len(set(names)):
        raise RuntimeError("renaming would create duplicate cc-connect project names")
    backup = _atomic_replace(path, updated, ".pre-project-rename.bak")
    return backup, renamed
