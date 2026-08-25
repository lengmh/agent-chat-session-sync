#!/usr/bin/env python3
"""Fail when a release archive is incomplete, non-portable, or has bad checksums."""

from __future__ import annotations

import ast
import hashlib
import os
from pathlib import Path
import runpy
import subprocess
import sys
import tarfile
import zipfile


ROOT = Path(__file__).resolve().parents[1]
CC_CONNECT_REVISION = "5d4c96dd12774574369e75b60084140101c9a59a"


def expected_artifact_names() -> set[str]:
    namespace = runpy.run_path(
        str(ROOT / "src" / "agent_chat_session_sync" / "__init__.py")
    )
    version = namespace["__version__"]
    return {
        f"agent_chat_session_sync-{version}-py3-none-any.whl",
        f"agent_chat_session_sync-{version}.tar.gz",
        "cc-connect-windows-x64.exe",
        "SHA256SUMS",
    }


def expected_commit() -> str:
    explicit = os.environ.get("ACSS_EXPECTED_COMMIT", "").strip()
    if explicit:
        return explicit
    try:
        return subprocess.check_output(
            ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.SubprocessError) as error:
        raise SystemExit("cannot determine expected release commit") from error


def parse_build_info(source: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for statement in ast.parse(source).body:
        if (
            isinstance(statement, ast.Assign)
            and len(statement.targets) == 1
            and isinstance(statement.targets[0], ast.Name)
            and statement.targets[0].id in {"GIT_COMMIT", "BUILD_SOURCE"}
        ):
            value = ast.literal_eval(statement.value)
            if isinstance(value, str):
                values[statement.targets[0].id] = value
    return values


def main() -> int:
    directory = Path(sys.argv[1] if len(sys.argv) > 1 else "dist")
    expected_names = expected_artifact_names()
    actual_names = {path.name for path in directory.iterdir()}
    missing = sorted(expected_names - actual_names)
    if missing:
        raise SystemExit(f"missing release artifacts: {', '.join(missing)}")
    unexpected = sorted(actual_names - expected_names)
    if unexpected:
        raise SystemExit(f"unexpected release artifacts: {', '.join(unexpected)}")

    executable = directory / "cc-connect-windows-x64.exe"
    wheel = directory / next(
        name for name in expected_names if name.endswith(".whl")
    )
    sdist = directory / next(
        name for name in expected_names if name.endswith(".tar.gz")
    )

    with zipfile.ZipFile(wheel) as archive:
        build_info = archive.read("agent_chat_session_sync/_build_info.py").decode("utf-8")
        build_values = parse_build_info(build_info)
        commit = expected_commit()
        if build_values.get("GIT_COMMIT") != commit:
            raise SystemExit("wheel build commit does not match expected commit")
        if build_values.get("BUILD_SOURCE") != f"git:{commit}":
            raise SystemExit("wheel BUILD_SOURCE does not match expected commit")

    with tarfile.open(sdist) as archive:
        names = archive.getnames()
        build_info_names = [
            name
            for name in names
            if name.endswith("/src/agent_chat_session_sync/_build_info.py")
        ]
        if len(build_info_names) != 1:
            raise SystemExit("sdist must contain exactly one stamped _build_info.py")
        build_member = archive.extractfile(build_info_names[0])
        if build_member is None:
            raise SystemExit("cannot read sdist build identity")
        sdist_build_values = parse_build_info(
            build_member.read().decode("utf-8")
        )
        if sdist_build_values.get("GIT_COMMIT") != commit:
            raise SystemExit("sdist build commit does not match expected commit")
        if sdist_build_values.get("BUILD_SOURCE") != f"git:{commit}":
            raise SystemExit("sdist BUILD_SOURCE does not match expected commit")
        for required in ("/patches/", "/scripts/", "/docs/"):
            if not any(required in name for name in names):
                raise SystemExit(f"sdist is missing {required.strip('/')}")
        required_installer = "scripts/install-windows.ps1"
        if not any(name.endswith(f"/{required_installer}") for name in names):
            raise SystemExit(f"sdist is missing {required_installer}")

    executable_bytes = executable.read_bytes()
    if executable.stat().st_size < 4096 or executable_bytes[:2] != b"MZ":
        raise SystemExit("invalid Windows executable: expected nontrivial PE/MZ file")
    executable_provenance = (
        f"acss:{commit};upstream:{CC_CONNECT_REVISION}".encode("ascii")
    )
    if executable_provenance not in executable_bytes:
        raise SystemExit(
            "Windows executable build commit does not match expected commit"
        )

    checksum_file = directory / "SHA256SUMS"
    entries = {}
    for line in checksum_file.read_text(encoding="utf-8").splitlines():
        digest, name = line.split("  ", 1)
        if Path(name).name != name:
            raise SystemExit(f"unsafe checksum path: {name}")
        if name in entries:
            raise SystemExit(f"duplicate checksum entry: {name}")
        entries[name] = digest
    required = expected_names - {"SHA256SUMS"}
    missing_checksums = sorted(required - entries.keys())
    if missing_checksums:
        raise SystemExit(
            f"SHA256SUMS is missing entries: {', '.join(missing_checksums)}"
        )
    unexpected_checksums = sorted(entries.keys() - required)
    if unexpected_checksums:
        raise SystemExit(
            f"SHA256SUMS has unexpected entries: {', '.join(unexpected_checksums)}"
        )
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
