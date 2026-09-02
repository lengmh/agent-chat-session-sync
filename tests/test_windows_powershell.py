from pathlib import Path
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest


class WindowsPowerShellTests(unittest.TestCase):
    @staticmethod
    def _installer_script() -> Path:
        return Path(__file__).resolve().parents[1] / "scripts" / "install-windows.ps1"

    def _git_usr_tool(self, name: str) -> Path:
        git = shutil.which("git")
        if not git:
            self.skipTest("Git for Windows is required")
        tool = Path(git).resolve().parent.parent / "usr" / "bin" / f"{name}.exe"
        if not tool.is_file():
            self.skipTest(f"Git for Windows {name}.exe is required")
        return tool

    @staticmethod
    def _git_tool_environment(tool: Path) -> dict[str, str]:
        environment = os.environ.copy()
        environment["PATH"] = str(tool.parent) + os.pathsep + environment["PATH"]
        return environment

    @staticmethod
    def _external_context(
        backup_dir: Path,
        state_root: Path,
        target: Path,
        acss: Path | str = "",
        **updates: object,
    ) -> dict[str, object]:
        context: dict[str, object] = {
            "backup_dir": str(backup_dir),
            "status": "external_lifecycle_pending_doctor",
            "lifecycle_mode": "external",
            "doctor_status": "deferred",
            "doctor_required": True,
            "external_lifecycle_target": str(target),
            "files": [],
            "task_changed": False,
            "task_snapshot": {},
            "venv_path": str(state_root / "venv"),
            "venv_intent": False,
            "venv_new_installed": False,
            "binary_existed": False,
            "binary_intent": True,
            "binary_backup_moved": False,
            "binary_new_installed": True,
            "cc_connect_snapshot": {
                "existed": False,
                "installed": False,
                "running": False,
            },
            "cc_connect_stopped": False,
            "cc_connect_new_started": False,
            "cc_connect_target": str(target),
            "acss_executable": str(acss),
        }
        context.update(updates)
        return context

    def _run_installer(
        self,
        source: Path,
        arguments: list[str],
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                str(shutil.which("pwsh.exe")),
                "-NoProfile",
                "-NonInteractive",
                "-File",
                str(source),
                *arguments,
            ],
            cwd=cwd,
            text=True,
            capture_output=True,
            check=False,
            env=env,
        )

    def _clean_installer_fixture(self, fixture: Path, source: Path) -> Path:
        scripts = fixture / "scripts"
        scripts.mkdir()
        script = scripts / source.name
        shutil.copy2(source, script)
        subprocess.run(["git", "init", "-q"], cwd=fixture, check=True)
        subprocess.run(
            ["git", "config", "user.name", "ACSS Test"],
            cwd=fixture,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "acss-test@example.invalid"],
            cwd=fixture,
            check=True,
        )
        return script

    def test_install_script_has_transactional_windows_contract(self) -> None:
        script = (
            Path(__file__).resolve().parents[1] / "scripts" / "install-windows.ps1"
        ).read_text(encoding="utf-8")

        self.assertIn(
            "[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'High')]",
            script,
        )
        self.assertIn("$ErrorActionPreference = 'Stop'", script)
        self.assertIn("PowerShell 7 or newer is required", script)
        self.assertIn("Windows x64 only", script)
        self.assertIn("Get-FileHash", script)
        self.assertIn("-Algorithm SHA256", script)
        self.assertIn("[string]$PythonPackage", script)
        self.assertIn("[string]$ExpectedPythonSha256", script)
        self.assertIn("$pythonInstallSource", script)
        self.assertIn("staged-python-package.whl", script)
        self.assertIn("staged-cc-connect.exe", script)
        self.assertIn("installed cc-connect SHA-256 mismatch", script)
        self.assertIn("installed Python package commit mismatch", script)
        self.assertIn("$uv = (Get-Command uv -ErrorAction Stop).Source", script)
        self.assertIn(
            "Invoke-Checked $uv @('venv', $stagedVenv, '--python', '3.11')",
            script,
        )
        self.assertIn(
            "Invoke-Checked $uv @(\n                'pip'\n                'install'",
            script,
        )
        self.assertIn("Python package and Windows configuration", script)
        self.assertIn("Codex and Claude Code Hooks", script)
        self.assertIn("worker Scheduled Task", script)
        self.assertIn("cc-connect binary", script)
        self.assertIn("manifest.json", script)
        self.assertIn("Rollback-Operation", script)
        self.assertIn("RollbackManifest", script)
        self.assertIn("[Environment]::Is64BitProcess", script)
        self.assertNotIn("Invoke-Expression", script)
        self.assertNotIn("Remove-Item -Recurse", script)

    def test_external_lifecycle_defers_but_keeps_doctor_gate(self) -> None:
        script = (
            Path(__file__).resolve().parents[1] / "scripts" / "install-windows.ps1"
        ).read_text(encoding="utf-8")

        self.assertIn(
            "$context.status = 'external_lifecycle_pending_doctor'",
            script,
        )
        self.assertIn("$context.doctor_status = 'deferred'", script)
        self.assertIn("$context.doctor_required = $true", script)
        self.assertIn("$context.external_lifecycle_target = $CcConnectTarget", script)
        self.assertIn(
            "ExternalCcConnectLifecycle requires CcConnectBinary, CcConnectTarget, and ExpectedCcConnectSha256.",
            script,
        )
        self.assertIn(
            "ExternalCcConnectLifecycle cannot be used with RestartCcConnect.",
            script,
        )
        self.assertIn(
            "if ($doTask -and -not $ExternalCcConnectLifecycle) {",
            script,
        )
        self.assertIn("Invoke-Checked $context.acss_executable @('doctor')", script)

    @unittest.skipUnless(
        os.name == "nt"
        and shutil.which("pwsh.exe")
        and shutil.which("git")
        and shutil.which("uv"),
        "Windows PowerShell, git, and uv are required",
    )
    def test_external_lifecycle_whatif_does_not_create_state_root(self) -> None:
        source = self._installer_script()
        with tempfile.TemporaryDirectory() as raw:
            fixture = Path(raw)
            script = self._clean_installer_fixture(fixture, source)
            candidate = fixture / "agent_chat_session_sync-0.6.0a1-py3-none-any.whl"
            candidate.write_bytes(b"candidate-wheel")
            candidate_hash = hashlib.sha256(candidate.read_bytes()).hexdigest()
            cc_connect = fixture / "cc-connect-windows-x64.exe"
            cc_connect.write_bytes(b"candidate-cc-connect")
            cc_connect_hash = hashlib.sha256(cc_connect.read_bytes()).hexdigest()
            subprocess.run(["git", "add", "."], cwd=fixture, check=True)
            subprocess.run(
                ["git", "commit", "-q", "-m", "fixture"],
                cwd=fixture,
                check=True,
            )
            state_root = fixture / "state"
            result = self._run_installer(
                script,
                [
                    "-WhatIf",
                    "-StateRoot",
                    str(state_root),
                    "-TempRoot",
                    str(fixture / "temp"),
                    "-PythonPackage",
                    str(candidate),
                    "-ExpectedPythonSha256",
                    candidate_hash,
                    "-ExternalCcConnectLifecycle",
                    "-CcConnectBinary",
                    str(cc_connect),
                    "-CcConnectTarget",
                    str(fixture / "current-cc-connect.exe"),
                    "-ExpectedCcConnectSha256",
                    cc_connect_hash,
                ],
                cwd=fixture,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(state_root.exists())
            self.assertIn("No changes selected.", result.stdout)

    @unittest.skipUnless(
        os.name == "nt"
        and shutil.which("pwsh.exe")
        and shutil.which("git")
        and shutil.which("uv"),
        "Windows PowerShell, git, and uv are required",
    )
    def test_external_lifecycle_rejects_restart_cc_connect(self) -> None:
        source = self._installer_script()
        with tempfile.TemporaryDirectory() as raw:
            fixture = Path(raw)
            script = self._clean_installer_fixture(fixture, source)
            cc_connect = fixture / "cc-connect-windows-x64.exe"
            cc_connect.write_bytes(b"candidate-cc-connect")
            cc_connect_hash = hashlib.sha256(cc_connect.read_bytes()).hexdigest()
            subprocess.run(["git", "add", "."], cwd=fixture, check=True)
            subprocess.run(
                ["git", "commit", "-q", "-m", "fixture"],
                cwd=fixture,
                check=True,
            )

            result = self._run_installer(
                script,
                [
                    "-WhatIf",
                    "-ExternalCcConnectLifecycle",
                    "-RestartCcConnect",
                    "-CcConnectBinary",
                    str(cc_connect),
                    "-CcConnectTarget",
                    str(fixture / "current-cc-connect.exe"),
                    "-ExpectedCcConnectSha256",
                    cc_connect_hash,
                ],
                cwd=fixture,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn(
                "ExternalCcConnectLifecycle cannot be used with RestartCcConnect.",
                result.stderr,
            )

    @unittest.skipUnless(
        os.name == "nt"
        and shutil.which("pwsh.exe")
        and shutil.which("git")
        and shutil.which("uv"),
        "Windows PowerShell, git, and uv are required",
    )
    def test_external_lifecycle_requires_target_to_be_stopped(self) -> None:
        source = self._installer_script()
        sleep_exe = self._git_usr_tool("sleep")

        with tempfile.TemporaryDirectory() as raw:
            fixture = Path(raw)
            script = self._clean_installer_fixture(fixture, source)
            candidate = fixture / "cc-connect-windows-x64.exe"
            candidate.write_bytes(b"candidate-cc-connect")
            candidate_hash = hashlib.sha256(candidate.read_bytes()).hexdigest()
            target = fixture / "cc-connect.exe"
            shutil.copy2(sleep_exe, target)
            subprocess.run(["git", "add", "."], cwd=fixture, check=True)
            subprocess.run(
                ["git", "commit", "-q", "-m", "fixture"],
                cwd=fixture,
                check=True,
            )
            environment = self._git_tool_environment(sleep_exe)
            process = subprocess.Popen(
                [str(target), "30"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=environment,
            )
            try:
                self.assertIsNone(process.poll(), "temporary cc-connect target did not start")
                result = self._run_installer(
                    script,
                    [
                        "-WhatIf",
                        "-StateRoot",
                        str(fixture / "state"),
                        "-ExternalCcConnectLifecycle",
                        "-CcConnectBinary",
                        str(candidate),
                        "-CcConnectTarget",
                        str(target),
                        "-ExpectedCcConnectSha256",
                        candidate_hash,
                    ],
                    cwd=fixture,
                    env=environment,
                )
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=5)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn(
                "Stop cc-connect through its external lifecycle before replacing the binary.",
                result.stderr,
            )

    @unittest.skipUnless(
        os.name == "nt" and shutil.which("pwsh.exe"),
        "Windows PowerShell is required",
    )
    def test_rollback_whatif_does_not_modify_manifest_or_state(self) -> None:
        source = (
            Path(__file__).resolve().parents[1] / "scripts" / "install-windows.ps1"
        )
        with tempfile.TemporaryDirectory() as raw:
            fixture = Path(raw)
            state_root = fixture / "state"
            backup_dir = state_root / "backups" / "operation"
            backup_dir.mkdir(parents=True)
            manifest = backup_dir / "manifest.json"
            context = {
                "backup_dir": str(backup_dir),
                "status": "failed",
                "files": [],
                "task_changed": False,
                "task_snapshot": {},
                "venv_path": str(state_root / "venv"),
                "venv_intent": False,
                "venv_new_installed": False,
                "binary_existed": False,
                "binary_intent": False,
                "binary_backup_moved": False,
                "binary_new_installed": False,
                "cc_connect_snapshot": {
                    "existed": False,
                    "installed": False,
                    "running": False,
                },
                "cc_connect_stopped": False,
                "cc_connect_new_started": False,
                "cc_connect_target": str(state_root / "cc-connect.exe"),
            }
            original = json.dumps(context, sort_keys=True).encode()
            manifest.write_bytes(original)

            result = subprocess.run(
                [
                    str(shutil.which("pwsh.exe")),
                    "-NoProfile",
                    "-NonInteractive",
                    "-File",
                    str(source),
                    "-WhatIf",
                    "-StateRoot",
                    str(state_root),
                    "-RollbackManifest",
                    str(manifest),
                ],
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(manifest.read_bytes(), original)
            self.assertIn("What if:", result.stdout)

    @unittest.skipUnless(
        os.name == "nt" and shutil.which("pwsh.exe") and shutil.which("git"),
        "Windows PowerShell and Git for Windows are required",
    )
    def test_complete_external_lifecycle_marks_manifest_after_doctor(self) -> None:
        source = self._installer_script()
        true_exe = self._git_usr_tool("true")

        with tempfile.TemporaryDirectory() as raw:
            fixture = Path(raw)
            state_root = fixture / "state"
            backup_dir = state_root / "backups" / "operation"
            backup_dir.mkdir(parents=True)
            acss_dir = state_root / "venv" / "Scripts"
            acss_dir.mkdir(parents=True)
            acss = acss_dir / "agent-chat-session-sync.exe"
            shutil.copy2(true_exe, acss)
            target = fixture / "cc-connect.exe"
            manifest = backup_dir / "manifest.json"
            context = self._external_context(backup_dir, state_root, target, acss)
            manifest.write_text(json.dumps(context), encoding="utf-8")

            result = self._run_installer(
                source,
                [
                    "-StateRoot",
                    str(state_root),
                    "-CcConnectTarget",
                    str(target),
                    "-RollbackManifest",
                    str(manifest),
                    "-CompleteExternalLifecycle",
                    "-Confirm:$false",
                ],
                env=self._git_tool_environment(true_exe),
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            completed = json.loads(manifest.read_text(encoding="utf-8"))
            self.assertEqual(completed["status"], "applied")
            self.assertEqual(completed["doctor_status"], "passed")
            self.assertFalse(completed["doctor_required"])
            self.assertTrue(completed["external_lifecycle_completed_at"])

    @unittest.skipUnless(
        os.name == "nt" and shutil.which("pwsh.exe") and shutil.which("git"),
        "Windows PowerShell and Git for Windows are required",
    )
    def test_complete_external_lifecycle_records_failed_doctor_and_allows_retry(
        self,
    ) -> None:
        source = self._installer_script()
        false_exe = self._git_usr_tool("false")

        with tempfile.TemporaryDirectory() as raw:
            fixture = Path(raw)
            state_root = fixture / "state"
            backup_dir = state_root / "backups" / "operation"
            backup_dir.mkdir(parents=True)
            acss_dir = state_root / "venv" / "Scripts"
            acss_dir.mkdir(parents=True)
            acss = acss_dir / "agent-chat-session-sync.exe"
            shutil.copy2(false_exe, acss)
            target = fixture / "cc-connect.exe"
            manifest = backup_dir / "manifest.json"
            context = self._external_context(backup_dir, state_root, target, acss)
            manifest.write_text(json.dumps(context), encoding="utf-8")

            result = self._run_installer(
                source,
                [
                    "-StateRoot",
                    str(state_root),
                    "-CcConnectTarget",
                    str(target),
                    "-RollbackManifest",
                    str(manifest),
                    "-CompleteExternalLifecycle",
                    "-Confirm:$false",
                ],
                env=self._git_tool_environment(false_exe),
            )

            self.assertNotEqual(result.returncode, 0)
            failed = json.loads(manifest.read_text(encoding="utf-8"))
            self.assertEqual(failed["status"], "external_lifecycle_pending_doctor")
            self.assertEqual(failed["doctor_status"], "failed")
            self.assertTrue(failed["doctor_required"])
            self.assertTrue(failed["external_lifecycle_last_attempt_at"])

            true_exe = self._git_usr_tool("true")
            shutil.copy2(true_exe, acss)
            retried = self._run_installer(
                source,
                [
                    "-StateRoot",
                    str(state_root),
                    "-CcConnectTarget",
                    str(target),
                    "-RollbackManifest",
                    str(manifest),
                    "-CompleteExternalLifecycle",
                    "-Confirm:$false",
                ],
                env=self._git_tool_environment(true_exe),
            )
            self.assertEqual(retried.returncode, 0, retried.stderr)
            completed = json.loads(manifest.read_text(encoding="utf-8"))
            self.assertEqual(completed["status"], "applied")
            self.assertEqual(completed["doctor_status"], "passed")
            self.assertFalse(completed["doctor_required"])

    @unittest.skipUnless(
        os.name == "nt" and shutil.which("pwsh.exe"),
        "Windows PowerShell is required",
    )
    def test_complete_external_lifecycle_records_missing_console_script(self) -> None:
        source = self._installer_script()
        with tempfile.TemporaryDirectory() as raw:
            fixture = Path(raw)
            state_root = fixture / "state"
            backup_dir = state_root / "backups" / "operation"
            backup_dir.mkdir(parents=True)
            target = fixture / "cc-connect.exe"
            manifest = backup_dir / "manifest.json"
            missing_acss = state_root / "venv" / "Scripts" / "agent-chat-session-sync.exe"
            context = self._external_context(
                backup_dir,
                state_root,
                target,
                missing_acss,
            )
            manifest.write_text(json.dumps(context), encoding="utf-8")

            result = self._run_installer(
                source,
                [
                    "-StateRoot",
                    str(state_root),
                    "-CcConnectTarget",
                    str(target),
                    "-RollbackManifest",
                    str(manifest),
                    "-CompleteExternalLifecycle",
                    "-Confirm:$false",
                ],
            )

            self.assertNotEqual(result.returncode, 0)
            failed = json.loads(manifest.read_text(encoding="utf-8"))
            self.assertEqual(failed["doctor_status"], "failed")
            self.assertTrue(failed["doctor_required"])
            self.assertTrue(failed["external_lifecycle_last_attempt_at"])

    @unittest.skipUnless(
        os.name == "nt" and shutil.which("pwsh.exe") and shutil.which("git"),
        "Windows PowerShell and Git for Windows are required",
    )
    def test_external_lifecycle_rollback_does_not_control_cc_connect(self) -> None:
        source = self._installer_script()
        echo_exe = self._git_usr_tool("echo")

        with tempfile.TemporaryDirectory() as raw:
            fixture = Path(raw)
            state_root = fixture / "state"
            backup_dir = state_root / "backups" / "operation"
            backup_dir.mkdir(parents=True)
            target = fixture / "cc-connect.exe"
            shutil.copy2(echo_exe, target)
            backup_binary = backup_dir / "cc-connect.exe"
            shutil.copy2(echo_exe, backup_binary)

            manifest = backup_dir / "manifest.json"
            context = self._external_context(
                backup_dir,
                state_root,
                target,
                binary_existed=True,
                binary_backup_moved=True,
                cc_connect_snapshot={
                    "existed": True,
                    "installed": True,
                    "running": True,
                },
                cc_connect_stopped=True,
                cc_connect_new_started=True,
            )
            manifest.write_text(json.dumps(context), encoding="utf-8")

            result = self._run_installer(
                source,
                [
                    "-StateRoot",
                    str(state_root),
                    "-CcConnectTarget",
                    str(target),
                    "-RollbackManifest",
                    str(manifest),
                    "-ExternalCcConnectLifecycle",
                    "-Confirm:$false",
                ],
                env=self._git_tool_environment(echo_exe),
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("daemon stop", result.stdout)
            self.assertNotIn("daemon start", result.stdout)
            self.assertTrue(target.is_file())
            self.assertEqual(
                json.loads(manifest.read_text(encoding="utf-8"))["status"],
                "rolled_back",
            )

    @unittest.skipUnless(
        os.name == "nt" and shutil.which("pwsh.exe") and shutil.which("git"),
        "Windows PowerShell and Git for Windows are required",
    )
    def test_external_lifecycle_rollback_rejects_nonexternal_manifest(self) -> None:
        source = self._installer_script()
        echo_exe = self._git_usr_tool("echo")

        with tempfile.TemporaryDirectory() as raw:
            fixture = Path(raw)
            state_root = fixture / "state"
            backup_dir = state_root / "backups" / "operation"
            backup_dir.mkdir(parents=True)
            target = fixture / "cc-connect.exe"
            shutil.copy2(echo_exe, target)
            shutil.copy2(echo_exe, backup_dir / "cc-connect.exe")
            manifest = backup_dir / "manifest.json"
            context = self._external_context(
                backup_dir,
                state_root,
                target,
                binary_existed=True,
                binary_backup_moved=True,
                cc_connect_snapshot={
                    "existed": True,
                    "installed": True,
                    "running": True,
                },
                cc_connect_stopped=True,
                cc_connect_new_started=True,
                lifecycle_mode="installer",
                status="applied",
                doctor_status="passed",
                doctor_required=False,
            )
            manifest.write_text(json.dumps(context), encoding="utf-8")

            result = self._run_installer(
                source,
                [
                    "-StateRoot",
                    str(state_root),
                    "-CcConnectTarget",
                    str(target),
                    "-RollbackManifest",
                    str(manifest),
                    "-ExternalCcConnectLifecycle",
                    "-Confirm:$false",
                ],
                env=self._git_tool_environment(echo_exe),
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn(
                "RollbackManifest is not an external lifecycle operation.",
                result.stderr,
            )

    @unittest.skipUnless(
        os.name == "nt" and shutil.which("pwsh.exe"),
        "Windows PowerShell is required",
    )
    def test_rollback_rejects_manifest_with_external_venv_path(self) -> None:
        source = (
            Path(__file__).resolve().parents[1] / "scripts" / "install-windows.ps1"
        )
        with tempfile.TemporaryDirectory() as raw:
            fixture = Path(raw)
            state_root = fixture / "state"
            backup_dir = state_root / "backups" / "operation"
            backup_dir.mkdir(parents=True)
            external = fixture / "external-venv"
            external.mkdir()
            marker = external / "marker.txt"
            marker.write_text("keep", encoding="utf-8")
            manifest = backup_dir / "manifest.json"
            context = {
                "backup_dir": str(backup_dir),
                "status": "failed",
                "files": [],
                "task_changed": False,
                "task_snapshot": {},
                "venv_path": str(external),
                "venv_intent": True,
                "venv_new_installed": True,
                "binary_existed": False,
                "binary_intent": False,
                "binary_backup_moved": False,
                "binary_new_installed": False,
                "cc_connect_snapshot": {
                    "existed": False,
                    "installed": False,
                    "running": False,
                },
                "cc_connect_stopped": False,
                "cc_connect_new_started": False,
                "cc_connect_target": "",
            }
            original = json.dumps(context, sort_keys=True).encode()
            manifest.write_bytes(original)

            result = subprocess.run(
                [
                    str(shutil.which("pwsh.exe")),
                    "-NoProfile",
                    "-NonInteractive",
                    "-File",
                    str(source),
                    "-StateRoot",
                    str(state_root),
                    "-RollbackManifest",
                    str(manifest),
                    "-Confirm:$false",
                ],
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("venv_path does not match StateRoot", result.stderr)
            self.assertEqual(marker.read_text(encoding="utf-8"), "keep")
            self.assertEqual(manifest.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
