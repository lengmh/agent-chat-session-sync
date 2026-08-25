#!/usr/bin/env python3
"""Write deterministic SHA-256 checksums for release artifacts."""

from __future__ import annotations

import hashlib
from pathlib import Path
import runpy
import sys


ROOT = Path(__file__).resolve().parents[1]


def expected_artifact_names() -> set[str]:
    namespace = runpy.run_path(
        str(ROOT / "src" / "agent_chat_session_sync" / "__init__.py")
    )
    version = namespace["__version__"]
    return {
        f"agent_chat_session_sync-{version}-py3-none-any.whl",
        f"agent_chat_session_sync-{version}.tar.gz",
        "cc-connect-windows-x64.exe",
    }


def release_artifacts(directory: Path) -> list[Path]:
    expected = expected_artifact_names()
    actual = {path.name for path in directory.iterdir()}
    missing = sorted(expected - actual)
    if missing:
        raise SystemExit(f"missing release artifacts: {', '.join(missing)}")
    unexpected = sorted(actual - expected - {"SHA256SUMS"})
    if unexpected:
        raise SystemExit(f"unexpected release artifacts: {', '.join(unexpected)}")
    return [directory / name for name in sorted(expected)]


def main() -> int:
    directory = Path(sys.argv[1] if len(sys.argv) > 1 else "dist")
    artifacts = release_artifacts(directory)
    lines = [f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}" for path in artifacts]
    output = directory / "SHA256SUMS"
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
