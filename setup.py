from __future__ import annotations

import ast
import os
from pathlib import Path
import subprocess

from setuptools import setup
from setuptools.command.build_py import build_py as _build_py
from setuptools.command.sdist import sdist as _sdist


BUILD_INFO = Path("src/agent_chat_session_sync/_build_info.py")


def stamped_source_commit() -> str | None:
    path = Path(__file__).parent / BUILD_INFO
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeError):
        return None
    values: dict[str, str] = {}
    for statement in tree.body:
        if (
            isinstance(statement, ast.Assign)
            and len(statement.targets) == 1
            and isinstance(statement.targets[0], ast.Name)
            and statement.targets[0].id in {"GIT_COMMIT", "BUILD_SOURCE"}
        ):
            value = ast.literal_eval(statement.value)
            if isinstance(value, str):
                values[statement.targets[0].id] = value
    commit = values.get("GIT_COMMIT", "")
    if commit and values.get("BUILD_SOURCE") == f"git:{commit}":
        return commit
    return None


def source_commit() -> str:
    explicit = os.environ.get("ACSS_BUILD_COMMIT", "").strip()
    if explicit:
        return explicit
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=Path(__file__).parent, text=True, stderr=subprocess.DEVNULL
        ).strip()
    except (OSError, subprocess.SubprocessError):
        return stamped_source_commit() or "UNKNOWN"


def write_build_info(target: Path, commit: str) -> None:
    target.write_text(
        f'GIT_COMMIT = {commit!r}\nBUILD_SOURCE = {("git:" + commit)!r}\n',
        encoding="utf-8",
    )


class build_py(_build_py):
    def run(self) -> None:
        super().run()
        target = Path(self.build_lib) / "agent_chat_session_sync" / "_build_info.py"
        write_build_info(target, source_commit())


class sdist(_sdist):
    def make_release_tree(self, base_dir: str, files: list[str]) -> None:
        super().make_release_tree(base_dir, files)
        write_build_info(
            Path(base_dir) / BUILD_INFO,
            source_commit(),
        )


setup(cmdclass={"build_py": build_py, "sdist": sdist})
