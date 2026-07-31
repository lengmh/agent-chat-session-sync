#!/usr/bin/env python3
"""Fail when a release archive is incomplete, non-portable, or has bad checksums."""

from __future__ import annotations

import hashlib
from pathlib import Path
import sys
import tarfile
import zipfile


def main() -> int:
    directory = Path(sys.argv[1] if len(sys.argv) > 1 else "dist")
    wheel = next(directory.glob("*.whl"))
    sdist = next(directory.glob("*.tar.gz"))

    with zipfile.ZipFile(wheel) as archive:
        build_info = archive.read("agent_chat_session_sync/_build_info.py").decode("utf-8")
        if "BUILD_SOURCE = 'git:" not in build_info and 'BUILD_SOURCE = "git:' not in build_info:
            raise SystemExit("wheel BUILD_SOURCE is not a deterministic git identity")

    with tarfile.open(sdist) as archive:
        names = archive.getnames()
        for required in ("/patches/", "/scripts/", "/docs/"):
            if not any(required in name for name in names):
                raise SystemExit(f"sdist is missing {required.strip('/')}")

    checksum_file = directory / "SHA256SUMS"
    entries = {}
    for line in checksum_file.read_text(encoding="utf-8").splitlines():
        digest, name = line.split("  ", 1)
        if Path(name).name != name:
            raise SystemExit(f"unsafe checksum path: {name}")
        entries[name] = digest
    required = {wheel.name, sdist.name}
    if not required <= entries.keys():
        raise SystemExit("SHA256SUMS is missing the wheel or source distribution")
    for name, expected in sorted(entries.items()):
        path = directory / name
        if not path.is_file():
            raise SystemExit(f"checksum entry has no artifact: {name}")
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if expected != actual:
            raise SystemExit(f"checksum mismatch: {name}")
    print(f"verified {wheel.name} and {sdist.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
