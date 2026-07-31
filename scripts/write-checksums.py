#!/usr/bin/env python3
"""Write deterministic SHA-256 checksums for release artifacts."""

from __future__ import annotations

import hashlib
from pathlib import Path
import sys


def release_artifacts(directory: Path) -> list[Path]:
    return sorted(
        path
        for path in directory.iterdir()
        if path.is_file()
        and (
            path.name.endswith((".whl", ".tar.gz"))
            or path.name == "cc-connect"
            or path.name.startswith("cc-connect-")
        )
    )


def main() -> int:
    directory = Path(sys.argv[1] if len(sys.argv) > 1 else "dist")
    artifacts = release_artifacts(directory)
    if not artifacts:
        raise SystemExit(f"no release artifacts found in {directory}")
    lines = [f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}" for path in artifacts]
    output = directory / "SHA256SUMS"
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
