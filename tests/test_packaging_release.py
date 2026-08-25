import hashlib
import io
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import unittest
import zipfile


ROOT = Path(__file__).resolve().parents[1]
VERSION = "0.6.0a1"
COMMIT = "0123456789abcdef0123456789abcdef01234567"
CC_CONNECT_REVISION = "5d4c96dd12774574369e75b60084140101c9a59a"


def _write_checksums(directory: Path, names: list[str]) -> None:
    lines = [
        f"{hashlib.sha256((directory / name).read_bytes()).hexdigest()}  {name}"
        for name in names
    ]
    (directory / "SHA256SUMS").write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8",
    )


def _write_release_fixture(
    directory: Path,
    *,
    include_exe: bool,
    include_windows_script: bool = True,
    sdist_commit: str = COMMIT,
    exe_commit: str = COMMIT,
) -> None:
    wheel_name = f"agent_chat_session_sync-{VERSION}-py3-none-any.whl"
    sdist_name = f"agent_chat_session_sync-{VERSION}.tar.gz"
    with zipfile.ZipFile(directory / wheel_name, "w") as archive:
        archive.writestr(
            "agent_chat_session_sync/_build_info.py",
            f'GIT_COMMIT = "{COMMIT}"\nBUILD_SOURCE = "git:{COMMIT}"\n',
        )
    with tarfile.open(directory / sdist_name, "w:gz") as archive:
        root = f"agent_chat_session_sync-{VERSION}"
        members = [
            (
                f"{root}/src/agent_chat_session_sync/_build_info.py",
                (
                    f'GIT_COMMIT = "{sdist_commit}"\n'
                    f'BUILD_SOURCE = "git:{sdist_commit}"\n'
                ).encode(),
            ),
            (f"{root}/patches/example.patch", b"patch"),
            (f"{root}/scripts/install.sh", b"#!/bin/sh"),
            (f"{root}/docs/ACCEPTANCE.md", b"acceptance"),
        ]
        if include_windows_script:
            members.append(
                (f"{root}/scripts/install-windows.ps1", b"Write-Host test")
            )
        for name, content in members:
            info = tarfile.TarInfo(name)
            info.size = len(content)
            archive.addfile(info, fileobj=io.BytesIO(content))
    names = [wheel_name, sdist_name]
    if include_exe:
        exe_name = "cc-connect-windows-x64.exe"
        provenance = (
            f"acss:{exe_commit};upstream:{CC_CONNECT_REVISION}".encode("ascii")
        )
        (directory / exe_name).write_bytes(
            b"MZ" + provenance + (b"\0" * (4096 - 2 - len(provenance)))
        )
        names.append(exe_name)
    _write_checksums(directory, names)


def _verify_release(directory: Path) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["ACSS_EXPECTED_COMMIT"] = COMMIT
    return subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "verify-release-artifacts.py"),
            str(directory),
        ],
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )


def _write_release_checksums(directory: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "write-checksums.py"),
            str(directory),
        ],
        text=True,
        capture_output=True,
        check=False,
    )


class PackagingReleaseTests(unittest.TestCase):
    def test_project_metadata_advertises_windows_and_python_314(self) -> None:
        metadata = tomllib.loads(
            (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        )["project"]

        self.assertEqual(metadata["requires-python"], ">=3.11")
        self.assertIn(
            "Operating System :: Microsoft :: Windows",
            metadata["classifiers"],
        )
        for version in ("3.11", "3.12", "3.13", "3.14"):
            self.assertIn(
                f"Programming Language :: Python :: {version}",
                metadata["classifiers"],
            )

    def test_package_version_matches_windows_alpha_milestone(self) -> None:
        namespace: dict[str, str] = {}
        exec(
            (ROOT / "src" / "agent_chat_session_sync" / "__init__.py").read_text(
                encoding="utf-8"
            ),
            namespace,
        )

        self.assertEqual(namespace["__version__"], "0.6.0a1")

    def test_source_distribution_includes_powershell_scripts(self) -> None:
        manifest = (ROOT / "MANIFEST.in").read_text(encoding="utf-8")

        self.assertIn("recursive-include scripts *.sh *.py *.ps1", manifest)

    def test_release_verifier_rejects_missing_windows_executable(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            _write_release_fixture(directory, include_exe=False)

            result = _verify_release(directory)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("cc-connect-windows-x64.exe", result.stderr)

    def test_release_verifier_accepts_exact_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            _write_release_fixture(directory, include_exe=True)

            result = _verify_release(directory)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("verified", result.stdout)

    def test_release_verifier_requires_windows_installer_in_sdist(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            _write_release_fixture(
                directory,
                include_exe=True,
                include_windows_script=False,
            )

            result = _verify_release(directory)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("scripts/install-windows.ps1", result.stderr)

    def test_release_verifier_rejects_extra_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            _write_release_fixture(directory, include_exe=True)
            (directory / "unexpected.txt").write_text("unexpected", encoding="utf-8")

            result = _verify_release(directory)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unexpected release artifacts", result.stderr)
        self.assertIn("unexpected.txt", result.stderr)

    def test_release_verifier_rejects_duplicate_checksum_entries(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            _write_release_fixture(directory, include_exe=True)
            checksum_file = directory / "SHA256SUMS"
            first_line = checksum_file.read_text(encoding="utf-8").splitlines()[0]
            checksum_file.write_text(
                checksum_file.read_text(encoding="utf-8") + first_line + "\n",
                encoding="utf-8",
            )

            result = _verify_release(directory)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("duplicate checksum entry", result.stderr)

    def test_release_verifier_rejects_mismatched_build_commit(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            _write_release_fixture(directory, include_exe=True)
            wheel = directory / (
                f"agent_chat_session_sync-{VERSION}-py3-none-any.whl"
            )
            with zipfile.ZipFile(wheel, "w") as archive:
                archive.writestr(
                    "agent_chat_session_sync/_build_info.py",
                    'GIT_COMMIT = "ffffffffffffffffffffffffffffffffffffffff"\n'
                    'BUILD_SOURCE = "git:ffffffffffffffffffffffffffffffffffffffff"\n',
                )
            _write_checksums(
                directory,
                [
                    wheel.name,
                    f"agent_chat_session_sync-{VERSION}.tar.gz",
                    "cc-connect-windows-x64.exe",
                ],
            )

            result = _verify_release(directory)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("wheel build commit does not match", result.stderr)

    def test_release_verifier_rejects_mismatched_sdist_commit(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            _write_release_fixture(
                directory,
                include_exe=True,
                sdist_commit="ffffffffffffffffffffffffffffffffffffffff",
            )

            result = _verify_release(directory)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("sdist build commit does not match", result.stderr)

    def test_source_distribution_carries_build_commit(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            env = os.environ.copy()
            env["ACSS_BUILD_COMMIT"] = COMMIT
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "build",
                    "--sdist",
                    "--outdir",
                    str(directory),
                    str(ROOT),
                ],
                text=True,
                capture_output=True,
                check=False,
                env=env,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            sdist = next(directory.glob("*.tar.gz"))
            with tarfile.open(sdist) as archive:
                name = next(
                    member
                    for member in archive.getnames()
                    if member.endswith(
                        "/src/agent_chat_session_sync/_build_info.py"
                    )
                )
                member = archive.extractfile(name)
                self.assertIsNotNone(member)
                source = member.read().decode("utf-8")

        self.assertIn(COMMIT, source)
        self.assertIn(f"git:{COMMIT}", source)

    def test_release_verifier_rejects_invalid_windows_executable(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            _write_release_fixture(directory, include_exe=True)
            executable = directory / "cc-connect-windows-x64.exe"
            executable.write_bytes(b"not-a-windows-executable")
            _write_checksums(
                directory,
                [
                    f"agent_chat_session_sync-{VERSION}-py3-none-any.whl",
                    f"agent_chat_session_sync-{VERSION}.tar.gz",
                    executable.name,
                ],
            )

            result = _verify_release(directory)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("invalid Windows executable", result.stderr)

    def test_release_verifier_rejects_mismatched_windows_executable_commit(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            _write_release_fixture(
                directory,
                include_exe=True,
                exe_commit="ffffffffffffffffffffffffffffffffffffffff",
            )

            result = _verify_release(directory)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "Windows executable build commit does not match",
            result.stderr,
        )

    def test_windows_cc_connect_build_embeds_release_provenance(self) -> None:
        script = (
            ROOT / "scripts" / "build-cc-connect-windows.ps1"
        ).read_text(encoding="utf-8")

        self.assertIn("[string]$BuildCommit", script)
        self.assertIn("acss:$BuildCommit;upstream:$revision", script)
        self.assertIn("'-ldflags'", script)
        self.assertIn("main.commit", script)

    def test_checksum_writer_rejects_unexpected_release_files(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            _write_release_fixture(directory, include_exe=True)
            (directory / "unexpected.txt").write_text("unexpected", encoding="utf-8")

            result = _write_release_checksums(directory)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unexpected release artifacts", result.stderr)

    def test_release_build_uses_locked_environment_and_prebuilt_windows_exe(
        self,
    ) -> None:
        script = (ROOT / "scripts" / "build-release.sh").read_text(
            encoding="utf-8"
        )

        self.assertIn("ACSS_WINDOWS_EXE", script)
        self.assertIn("uv run --locked --extra dev python -m build", script)
        self.assertIn("cc-connect-windows-x64.exe", script)
        self.assertIn("refusing existing release directory", script)
        self.assertIn("ACSS_EXPECTED_COMMIT", script)
        self.assertNotIn("rm -rf", script)

    def test_ci_matrix_covers_supported_python_and_operating_systems(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn("os: [ubuntu-latest, windows-latest]", workflow)
        self.assertIn(
            'python-version: ["3.11", "3.12", "3.13", "3.14"]',
            workflow,
        )
        self.assertIn("runs-on: ${{ matrix.os }}", workflow)
        self.assertIn("python -m unittest discover -s tests -v", workflow)
        self.assertIn("python -m compileall -q src tests", workflow)
        self.assertIn("Install and exercise wheel", workflow)
        self.assertIn("Install and exercise source distribution", workflow)
        self.assertIn("provenance --json", workflow)

    def test_ci_builds_and_integrates_patched_windows_cc_connect(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn("cc-connect-windows:", workflow)
        self.assertIn('go-version: "1.25.x"', workflow)
        self.assertIn("./scripts/build-cc-connect-windows.ps1", workflow)
        self.assertIn("cc-connect-windows-x64.exe", workflow)
        self.assertIn("ACSS_CC_CONNECT_SOURCE", workflow)
        self.assertIn("ACSS_GO", workflow)
        self.assertIn("ACSS_TEMP_DIR", workflow)
        self.assertIn(
            "python -m unittest tests.test_cc_connect_integration -v",
            workflow,
        )

    def test_ci_release_bundle_contains_exact_windows_alpha_artifacts(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn("needs: cc-connect-windows", workflow)
        self.assertIn("actions/download-artifact@v4", workflow)
        self.assertIn("ACSS_WINDOWS_EXE", workflow)
        self.assertIn("./scripts/build-release.sh", workflow)
        self.assertIn("dist/cc-connect-windows-x64.exe", workflow)
        self.assertIn("dist/SHA256SUMS", workflow)
        self.assertIn("if-no-files-found: error", workflow)

    def test_real_task_lifecycle_ci_is_manual_unique_and_self_cleaning(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn("workflow_dispatch:", workflow)
        self.assertIn("windows-task-lifecycle:", workflow)
        self.assertIn("if: github.event_name == 'workflow_dispatch'", workflow)
        self.assertIn("github.run_id", workflow)
        self.assertIn("github.run_attempt", workflow)
        self.assertIn("TASK_LOGON_INTERACTIVE_TOKEN", workflow)
        self.assertIn("TASK_RUNLEVEL_LUA", workflow)
        self.assertIn("TASK_INSTANCES_IGNORE_NEW", workflow)
        self.assertIn("DeleteTask", workflow)
        self.assertIn("DeleteFolder", workflow)
        self.assertIn("Task did not stop before cleanup", workflow)
        self.assertIn("finally", workflow)

    def test_public_docs_define_windows_alpha_support_and_release_boundary(
        self,
    ) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        security = (ROOT / "SECURITY.md").read_text(encoding="utf-8")
        acceptance = (ROOT / "docs" / "ACCEPTANCE.md").read_text(
            encoding="utf-8"
        )
        reliability = (ROOT / "docs" / "RELIABILITY.md").read_text(
            encoding="utf-8"
        )
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")

        self.assertIn("v0.6.0-alpha.1", readme)
        self.assertIn("Windows 11 x64 Alpha", readme)
        self.assertIn("PowerShell 7", readme)
        self.assertIn("Task Scheduler", readme)
        self.assertIn("Named Pipe", readme)
        self.assertIn("未进行 Authenticode 代码签名", readme)
        self.assertIn("-CcConnectBinary", readme)
        self.assertIn("-ExpectedCcConnectSha256", readme)
        self.assertIn("-PythonPackage", readme)
        self.assertIn("-ExpectedPythonSha256", readme)
        self.assertIn("scripts\\verify-release-artifacts.py", readme)
        self.assertIn("Windows 10", readme)
        self.assertIn("ARM64", readme)
        self.assertIn("Windows 11 x64 Alpha", security)
        self.assertIn("Named Pipe DACL", security)
        self.assertIn("unsigned", security)
        self.assertIn("candidate artifact", acceptance)
        self.assertIn("Named Pipe DACL", acceptance)
        self.assertIn("scripts\\verify-release-artifacts.py", acceptance)
        self.assertIn("-PythonPackage", acceptance)
        self.assertIn("$artifactDir = (Resolve-Path .\\dist).Path", acceptance)
        self.assertIn(
            "$checksumFile = Join-Path $artifactDir 'SHA256SUMS'",
            acceptance,
        )
        self.assertIn("Task Scheduler", reliability)
        self.assertIn("%LOCALAPPDATA%", reliability)
        self.assertIn("## [0.6.0-alpha.1]", changelog)
        self.assertIn("cc-connect-windows-x64.exe", changelog)


if __name__ == "__main__":
    unittest.main()
