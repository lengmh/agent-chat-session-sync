from pathlib import Path
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest


class WindowsPowerShellTests(unittest.TestCase):
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

    @unittest.skipUnless(
        os.name == "nt"
        and shutil.which("pwsh.exe")
        and shutil.which("git")
        and shutil.which("uv"),
        "Windows PowerShell, git, and uv are required",
    )
    def test_install_script_whatif_does_not_create_state_root(self) -> None:
        source = (
            Path(__file__).resolve().parents[1] / "scripts" / "install-windows.ps1"
        )
        with tempfile.TemporaryDirectory() as raw:
            fixture = Path(raw)
            scripts = fixture / "scripts"
            scripts.mkdir()
            shutil.copy2(source, scripts / source.name)
            candidate = fixture / "agent_chat_session_sync-0.6.0a1-py3-none-any.whl"
            candidate.write_bytes(b"candidate-wheel")
            candidate_hash = hashlib.sha256(candidate.read_bytes()).hexdigest()
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
            subprocess.run(["git", "add", source.name], cwd=scripts, check=True)
            subprocess.run(["git", "add", candidate.name], cwd=fixture, check=True)
            subprocess.run(
                ["git", "commit", "-q", "-m", "fixture"],
                cwd=fixture,
                check=True,
            )
            state_root = fixture / "state"
            result = subprocess.run(
                [
                    str(shutil.which("pwsh.exe")),
                    "-NoProfile",
                    "-NonInteractive",
                    "-File",
                    str(scripts / source.name),
                    "-WhatIf",
                    "-StateRoot",
                    str(state_root),
                    "-TempRoot",
                    str(fixture / "temp"),
                    "-PythonPackage",
                    str(candidate),
                    "-ExpectedPythonSha256",
                    candidate_hash,
                ],
                cwd=fixture,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(state_root.exists())
            self.assertIn("No changes selected.", result.stdout)

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
