from __future__ import annotations

import os
from pathlib import Path
import subprocess

from setuptools import setup
from setuptools.command.build_py import build_py as _build_py


def source_commit() -> str:
    explicit = os.environ.get("ACSS_BUILD_COMMIT", "").strip()
    if explicit:
        return explicit
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=Path(__file__).parent, text=True, stderr=subprocess.DEVNULL
        ).strip()
    except (OSError, subprocess.SubprocessError):
        return "UNKNOWN"


class build_py(_build_py):
    def run(self) -> None:
        super().run()
        target = Path(self.build_lib) / "agent_chat_session_sync" / "_build_info.py"
        target.write_text(
            f'GIT_COMMIT = {source_commit()!r}\nBUILD_SOURCE = {str(Path(__file__).parent.resolve())!r}\n',
            encoding="utf-8",
        )


setup(cmdclass={"build_py": build_py})
