from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path
import subprocess
import sys

from . import __version__
from ._build_info import BUILD_SOURCE, GIT_COMMIT


@dataclass(frozen=True)
class Provenance:
    service_version: str
    git_commit: str
    package_path: str
    python_path: str
    build_source: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


def current_provenance() -> Provenance:
    return Provenance(
        service_version=__version__,
        git_commit=GIT_COMMIT,
        package_path=str(Path(__file__).resolve().parent),
        python_path=str(Path(sys.executable).resolve()),
        build_source=BUILD_SOURCE,
    )


def source_head(path: Path) -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=path, text=True, stderr=subprocess.DEVNULL
        ).strip()
    except (OSError, subprocess.SubprocessError):
        return "UNKNOWN"


def provenance_json() -> str:
    return json.dumps(current_provenance().to_dict(), ensure_ascii=False, sort_keys=True)
