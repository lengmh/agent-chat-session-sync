from pathlib import Path
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
        self.assertIn("'venv'", script)
        self.assertIn("'uv', 'venv'", script)
        self.assertIn("'uv', 'pip', 'install'", script)
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
        os.name == "nt" and shutil.which("pwsh.exe") and shutil.which("git"),
        "Windows PowerShell and git are required",
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
                ],
                cwd=fixture,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(state_root.exists())
            self.assertIn("No changes selected.", result.stdout)


if __name__ == "__main__":
    unittest.main()
