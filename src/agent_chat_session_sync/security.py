from __future__ import annotations

import os
from pathlib import Path


def ensure_private_directory(path: Path) -> None:
    """Create a private data directory using the active platform policy.

    Windows ACL hardening is supplied by the Windows security provider. Until
    that provider is available, callers must rely on doctor failing closed and
    must not interpret POSIX mode bits as an ACL result.
    """

    path.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        path.chmod(0o700)


def harden_private_file(path: Path) -> None:
    if os.name != "nt":
        path.chmod(0o600)


def preserve_file_mode(source: Path, target: Path) -> None:
    if os.name != "nt":
        target.chmod(source.stat().st_mode & 0o777)
