from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ET

from agent_chat_session_sync.windows_tasks import (
    WINDOWS_WORKER_TASK_NAME,
    WINDOWS_WORKER_TASK_PATH,
    PowerShellTaskScheduler,
    install_windows_worker_task,
    worker_task_checks,
    uninstall_windows_worker_task,
    task_matches_worker_identity,
    render_worker_wrapper,
    render_worker_task_xml,
)


TASK_NS = {"task": "http://schemas.microsoft.com/windows/2004/02/mit/task"}


class FakeTaskScheduler:
    def __init__(self, existing_xml: str | None = None, state: str | None = None):
        self.existing_xml = existing_xml
        self.task_state = state
        self.registered: list[str] = []
        self.started = 0
        self.stopped = 0
        self.unregistered = 0
        self.operations: list[str] = []

    def export(self) -> str | None:
        self.operations.append("export")
        return self.existing_xml

    def register(self, xml: str) -> None:
        self.operations.append("register")
        self.registered.append(xml)

    def start(self) -> None:
        self.operations.append("start")
        self.started += 1

    def stop_and_wait(self) -> None:
        self.operations.append("stop_and_wait")
        self.stopped += 1

    def unregister(self) -> None:
        self.operations.append("unregister")
        self.unregistered += 1

    def state(self) -> str | None:
        self.operations.append("state")
        return self.task_state


class WindowsTaskTests(unittest.TestCase):
    def test_worker_task_is_limited_interactive_and_ignores_duplicates(self) -> None:
        xml = render_worker_task_xml(
            user_sid="S-1-5-21-1000",
            powershell=Path(r"C:\Program Files\PowerShell\7\pwsh.exe"),
            wrapper=Path(r"C:\Users\Example\AppData\Local\agent-chat-session-sync\service\worker.ps1"),
        )
        root = ET.fromstring(xml)

        self.assertEqual(WINDOWS_WORKER_TASK_PATH, "\\AgentChatSessionSync\\")
        self.assertEqual(WINDOWS_WORKER_TASK_NAME, "Worker")
        self.assertEqual(
            root.findtext("task:RegistrationInfo/task:URI", namespaces=TASK_NS),
            "\\AgentChatSessionSync\\Worker",
        )
        self.assertEqual(
            root.findtext("task:Principals/task:Principal/task:UserId", namespaces=TASK_NS),
            "S-1-5-21-1000",
        )
        self.assertEqual(
            root.findtext("task:Principals/task:Principal/task:LogonType", namespaces=TASK_NS),
            "InteractiveToken",
        )
        self.assertEqual(
            root.findtext("task:Principals/task:Principal/task:RunLevel", namespaces=TASK_NS),
            "LeastPrivilege",
        )
        self.assertEqual(
            root.findtext("task:Settings/task:MultipleInstancesPolicy", namespaces=TASK_NS),
            "IgnoreNew",
        )

    def test_worker_wrapper_stops_for_success_or_existing_worker(self) -> None:
        wrapper = render_worker_wrapper(
            executable=Path(r"C:\Program Files\ACSS\agent-chat-session-sync.exe"),
            environment={"ACSS_DATA_DIR": r"C:\Users\Example\AppData\Local\agent-chat-session-sync"},
        )

        self.assertIn("if ($exitCode -eq 0 -or $exitCode -eq 4)", wrapper)
        self.assertIn("exit $exitCode", wrapper)
        self.assertIn("Start-Sleep -Seconds 10", wrapper)

    def test_worker_wrapper_quotes_environment_without_exposing_legacy_socket(self) -> None:
        wrapper = render_worker_wrapper(
            executable=Path(r"C:\Program Files\ACSS\agent-chat-session-sync.exe"),
            environment={
                "ACSS_DATA_DIR": r"C:\Users\O'Brien\AppData\Local\agent-chat-session-sync",
                "CC_CONNECT_ENDPOINT": "npipe://./pipe/cc-connect-api-test",
            },
        )

        self.assertIn(
            "$env:ACSS_DATA_DIR = 'C:\\Users\\O''Brien\\AppData\\Local\\agent-chat-session-sync'",
            wrapper,
        )
        self.assertIn("$env:CC_CONNECT_ENDPOINT", wrapper)
        self.assertNotIn("CC_CONNECT_SOCKET", wrapper)

    @unittest.skipUnless(shutil.which("pwsh.exe"), "PowerShell 7 is required")
    def test_worker_wrapper_preserves_terminal_worker_exit_codes(self) -> None:
        for exit_code in (0, 4):
            with self.subTest(exit_code=exit_code), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                worker = root / "fake-worker.ps1"
                worker.write_text(
                    f"param([string]$Command)\nexit {exit_code}\n",
                    encoding="utf-8",
                )
                wrapper = root / "worker.ps1"
                wrapper.write_text(
                    render_worker_wrapper(
                        executable=worker,
                        environment={},
                    ),
                    encoding="utf-8",
                )

                result = subprocess.run(
                    [
                        str(shutil.which("pwsh.exe")),
                        "-NoProfile",
                        "-NonInteractive",
                        "-File",
                        str(wrapper),
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                )

            self.assertEqual(result.returncode, exit_code, result.stderr)

    def test_foreign_task_action_is_rejected(self) -> None:
        wrapper = Path(
            r"C:\Users\Example\AppData\Local\agent-chat-session-sync\service\worker.ps1"
        )
        xml = render_worker_task_xml(
            user_sid="S-1-5-21-1000",
            powershell=Path(r"C:\Program Files\PowerShell\7\pwsh.exe"),
            wrapper=wrapper,
        )
        foreign = xml.replace(
            r"C:\Program Files\PowerShell\7\pwsh.exe",
            r"C:\Windows\System32\not-ours.exe",
        )

        self.assertFalse(
            task_matches_worker_identity(
                foreign,
                user_sid="S-1-5-21-1000",
                powershell=Path(r"C:\Program Files\PowerShell\7\pwsh.exe"),
                wrapper=wrapper,
            )
        )

    def test_install_refuses_to_overwrite_foreign_task(self) -> None:
        powershell = Path(r"C:\Program Files\PowerShell\7\pwsh.exe")
        wrapper = Path(
            r"C:\Users\Example\AppData\Local\agent-chat-session-sync\service\worker.ps1"
        )
        foreign = render_worker_task_xml(
            user_sid="S-1-5-21-1000",
            powershell=Path(r"C:\Windows\System32\not-ours.exe"),
            wrapper=wrapper,
        )
        scheduler = FakeTaskScheduler(foreign)

        with tempfile.TemporaryDirectory() as raw:
            data_dir = Path(raw)
            with self.assertRaisesRegex(RuntimeError, "foreign Scheduled Task"):
                install_windows_worker_task(
                    data_dir,
                    executable=Path(r"C:\Program Files\ACSS\agent-chat-session-sync.exe"),
                    powershell=powershell,
                    user_sid="S-1-5-21-1000",
                    environment={},
                    scheduler=scheduler,
                )

            self.assertFalse((data_dir / "service" / "worker.ps1").exists())
        self.assertEqual(scheduler.registered, [])
        self.assertEqual(scheduler.started, 0)

    def test_install_writes_private_wrapper_registers_and_starts(self) -> None:
        scheduler = FakeTaskScheduler()

        with tempfile.TemporaryDirectory() as raw:
            data_dir = Path(raw)
            wrapper = install_windows_worker_task(
                data_dir,
                executable=Path(r"C:\Program Files\ACSS\agent-chat-session-sync.exe"),
                powershell=Path(r"C:\Program Files\PowerShell\7\pwsh.exe"),
                user_sid="S-1-5-21-1000",
                environment={"ACSS_DATA_DIR": str(data_dir)},
                scheduler=scheduler,
            )

            payload = wrapper.read_text(encoding="utf-8")
            self.assertIn("$env:ACSS_DATA_DIR", payload)
            self.assertIn("agent-chat-session-sync.exe", payload)
        self.assertEqual(len(scheduler.registered), 1)
        self.assertEqual(scheduler.started, 1)

    def test_install_refuses_to_overwrite_foreign_wrapper(self) -> None:
        scheduler = FakeTaskScheduler()

        with tempfile.TemporaryDirectory() as raw:
            data_dir = Path(raw)
            wrapper = data_dir / "service" / "worker.ps1"
            wrapper.parent.mkdir()
            wrapper.write_text("Write-Host 'not ours'\n", encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "foreign worker wrapper"):
                install_windows_worker_task(
                    data_dir,
                    executable=Path(r"C:\Program Files\ACSS\agent-chat-session-sync.exe"),
                    powershell=Path(r"C:\Program Files\PowerShell\7\pwsh.exe"),
                    user_sid="S-1-5-21-1000",
                    environment={},
                    scheduler=scheduler,
                )

            self.assertEqual(wrapper.read_text(encoding="utf-8"), "Write-Host 'not ours'\n")
        self.assertEqual(scheduler.registered, [])
        self.assertEqual(scheduler.started, 0)

    def test_install_restores_managed_wrapper_when_registration_fails(self) -> None:
        class FailingScheduler(FakeTaskScheduler):
            def register(self, xml: str) -> None:
                super().register(xml)
                raise RuntimeError("registration failed")

        scheduler = FailingScheduler()
        original = render_worker_wrapper(
            executable=Path(r"C:\Program Files\ACSS\old-agent-chat-session-sync.exe"),
            environment={"ACSS_DATA_DIR": r"C:\Old\Data"},
        )

        with tempfile.TemporaryDirectory() as raw:
            data_dir = Path(raw)
            wrapper = data_dir / "service" / "worker.ps1"
            wrapper.parent.mkdir()
            wrapper.write_text(original, encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "registration failed"):
                install_windows_worker_task(
                    data_dir,
                    executable=Path(r"C:\Program Files\ACSS\agent-chat-session-sync.exe"),
                    powershell=Path(r"C:\Program Files\PowerShell\7\pwsh.exe"),
                    user_sid="S-1-5-21-1000",
                    environment={},
                    scheduler=scheduler,
                )

            self.assertEqual(wrapper.read_text(encoding="utf-8"), original)
        self.assertEqual(scheduler.started, 0)

    def test_install_unregisters_new_task_when_start_fails(self) -> None:
        class StartFailingScheduler(FakeTaskScheduler):
            def start(self) -> None:
                super().start()
                raise RuntimeError("start failed")

        scheduler = StartFailingScheduler()

        with tempfile.TemporaryDirectory() as raw:
            data_dir = Path(raw)
            wrapper = data_dir / "service" / "worker.ps1"

            with self.assertRaisesRegex(RuntimeError, "start failed"):
                install_windows_worker_task(
                    data_dir,
                    executable=Path(r"C:\Program Files\ACSS\agent-chat-session-sync.exe"),
                    powershell=Path(r"C:\Program Files\PowerShell\7\pwsh.exe"),
                    user_sid="S-1-5-21-1000",
                    environment={},
                    scheduler=scheduler,
                )

            self.assertFalse(wrapper.exists())
        self.assertEqual(scheduler.unregistered, 1)

    def test_running_task_is_stopped_before_wrapper_update_and_restart(self) -> None:
        powershell = Path(r"C:\Program Files\PowerShell\7\pwsh.exe")

        with tempfile.TemporaryDirectory() as raw:
            data_dir = Path(raw)
            wrapper = data_dir / "service" / "worker.ps1"
            wrapper.parent.mkdir()
            wrapper.write_text(
                render_worker_wrapper(
                    executable=Path(r"C:\Program Files\ACSS\old.exe"),
                    environment={},
                ),
                encoding="utf-8",
            )
            existing_xml = render_worker_task_xml(
                user_sid="S-1-5-21-1000",
                powershell=powershell,
                wrapper=wrapper,
            )
            scheduler = FakeTaskScheduler(existing_xml, "Running")

            install_windows_worker_task(
                data_dir,
                executable=Path(r"C:\Program Files\ACSS\new.exe"),
                powershell=powershell,
                user_sid="S-1-5-21-1000",
                environment={},
                scheduler=scheduler,
            )

        self.assertLess(
            scheduler.operations.index("stop_and_wait"),
            scheduler.operations.index("register"),
        )
        self.assertEqual(scheduler.operations[-1], "start")

    def test_scheduler_export_treats_missing_task_as_absent(self) -> None:
        calls: list[list[str]] = []

        def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            calls.append(command)
            return subprocess.CompletedProcess(command, 3, "", "")

        scheduler = PowerShellTaskScheduler(
            Path(r"C:\Program Files\PowerShell\7\pwsh.exe"),
            run=run,
        )

        self.assertIsNone(scheduler.export())
        self.assertEqual(len(calls), 1)
        self.assertIn("Export-ScheduledTask", calls[0][-1])

    def test_scheduler_registration_creates_product_task_folder(self) -> None:
        calls: list[tuple[list[str], dict[str, object]]] = []

        def run(
            command: list[str],
            **kwargs: object,
        ) -> subprocess.CompletedProcess[str]:
            calls.append((command, kwargs))
            return subprocess.CompletedProcess(command, 0, "", "")

        scheduler = PowerShellTaskScheduler(
            Path(r"C:\Program Files\PowerShell\7\pwsh.exe"),
            run=run,
        )

        scheduler.register("<Task />")

        self.assertEqual(len(calls), 1)
        script = calls[0][0][-1]
        self.assertIn("CreateFolder('AgentChatSessionSync')", script)
        self.assertEqual(calls[0][1]["input"], "<Task />")
        self.assertEqual(calls[0][1]["encoding"], "utf-8")

    def test_worker_task_checks_require_owned_running_task_and_wrapper(self) -> None:
        powershell = Path(r"C:\Program Files\PowerShell\7\pwsh.exe")
        executable = Path(r"C:\Program Files\ACSS\agent-chat-session-sync.exe")
        environment = {"CC_CONNECT_ENDPOINT": "npipe://./pipe/cc-connect-api-test"}

        with tempfile.TemporaryDirectory() as raw:
            wrapper = Path(raw) / "service" / "worker.ps1"
            wrapper.parent.mkdir()
            wrapper.write_text(
                render_worker_wrapper(
                    executable=executable,
                    environment=environment,
                ),
                encoding="utf-8",
            )
            task_xml = render_worker_task_xml(
                user_sid="S-1-5-21-1000",
                powershell=powershell,
                wrapper=wrapper,
            )
            checks = worker_task_checks(
                wrapper=wrapper,
                executable=executable,
                powershell=powershell,
                user_sid="S-1-5-21-1000",
                environment=environment,
                scheduler=FakeTaskScheduler(task_xml, "Running"),
            )

        self.assertTrue(all(okay for _name, okay, _detail in checks))

    def test_uninstall_refuses_to_remove_foreign_task(self) -> None:
        powershell = Path(r"C:\Program Files\PowerShell\7\pwsh.exe")
        wrapper = Path(
            r"C:\Users\Example\AppData\Local\agent-chat-session-sync\service\worker.ps1"
        )
        foreign = render_worker_task_xml(
            user_sid="S-1-5-21-1000",
            powershell=Path(r"C:\Windows\System32\not-ours.exe"),
            wrapper=wrapper,
        )
        scheduler = FakeTaskScheduler(foreign)

        with self.assertRaisesRegex(RuntimeError, "foreign Scheduled Task"):
            uninstall_windows_worker_task(
                wrapper,
                powershell=powershell,
                user_sid="S-1-5-21-1000",
                scheduler=scheduler,
            )

        self.assertEqual(scheduler.stopped, 0)
        self.assertEqual(scheduler.unregistered, 0)

    def test_task_with_extra_action_is_foreign(self) -> None:
        powershell = Path(r"C:\Program Files\PowerShell\7\pwsh.exe")
        wrapper = Path(
            r"C:\Users\Example\AppData\Local\agent-chat-session-sync\service\worker.ps1"
        )
        xml = render_worker_task_xml(
            user_sid="S-1-5-21-1000",
            powershell=powershell,
            wrapper=wrapper,
        )
        root = ET.fromstring(xml)
        actions = root.find("task:Actions", TASK_NS)
        assert actions is not None
        extra = ET.SubElement(actions, f"{{{TASK_NS['task']}}}Exec")
        ET.SubElement(extra, f"{{{TASK_NS['task']}}}Command").text = "not-ours.exe"

        self.assertFalse(
            task_matches_worker_identity(
                ET.tostring(root, encoding="unicode"),
                user_sid="S-1-5-21-1000",
                powershell=powershell,
                wrapper=wrapper,
            )
        )

    def test_uninstall_refuses_foreign_wrapper_before_removing_task(self) -> None:
        powershell = Path(r"C:\Program Files\PowerShell\7\pwsh.exe")

        with tempfile.TemporaryDirectory() as raw:
            wrapper = Path(raw) / "service" / "worker.ps1"
            wrapper.parent.mkdir()
            wrapper.write_text("Write-Host 'not ours'\n", encoding="utf-8")
            owned = render_worker_task_xml(
                user_sid="S-1-5-21-1000",
                powershell=powershell,
                wrapper=wrapper,
            )
            scheduler = FakeTaskScheduler(owned)

            with self.assertRaisesRegex(RuntimeError, "foreign worker wrapper"):
                uninstall_windows_worker_task(
                    wrapper,
                    powershell=powershell,
                    user_sid="S-1-5-21-1000",
                    scheduler=scheduler,
                )

            self.assertTrue(wrapper.exists())
        self.assertEqual(scheduler.stopped, 0)
        self.assertEqual(scheduler.unregistered, 0)

    def test_uninstall_restores_wrapper_and_running_task_when_unregister_fails(self) -> None:
        class UnregisterFailingScheduler(FakeTaskScheduler):
            def unregister(self) -> None:
                super().unregister()
                raise RuntimeError("unregister failed")

        powershell = Path(r"C:\Program Files\PowerShell\7\pwsh.exe")
        payload = render_worker_wrapper(
            executable=Path(r"C:\Program Files\ACSS\agent-chat-session-sync.exe"),
            environment={},
        )

        with tempfile.TemporaryDirectory() as raw:
            wrapper = Path(raw) / "service" / "worker.ps1"
            wrapper.parent.mkdir()
            wrapper.write_text(payload, encoding="utf-8")
            task_xml = render_worker_task_xml(
                user_sid="S-1-5-21-1000",
                powershell=powershell,
                wrapper=wrapper,
            )
            scheduler = UnregisterFailingScheduler(task_xml, "Running")

            with self.assertRaisesRegex(RuntimeError, "unregister failed"):
                uninstall_windows_worker_task(
                    wrapper,
                    powershell=powershell,
                    user_sid="S-1-5-21-1000",
                    scheduler=scheduler,
                )

            self.assertEqual(wrapper.read_text(encoding="utf-8"), payload)
        self.assertEqual(scheduler.registered[-1], task_xml)
        self.assertEqual(scheduler.started, 1)

    def test_uninstall_removes_managed_wrapper_when_task_is_absent(self) -> None:
        scheduler = FakeTaskScheduler()
        powershell = Path(r"C:\Program Files\PowerShell\7\pwsh.exe")

        with tempfile.TemporaryDirectory() as raw:
            wrapper = Path(raw) / "service" / "worker.ps1"
            wrapper.parent.mkdir()
            wrapper.write_text(
                render_worker_wrapper(
                    executable=Path(r"C:\Program Files\ACSS\agent-chat-session-sync.exe"),
                    environment={},
                ),
                encoding="utf-8",
            )

            uninstall_windows_worker_task(
                wrapper,
                powershell=powershell,
                user_sid="S-1-5-21-1000",
                scheduler=scheduler,
            )

            self.assertFalse(wrapper.exists())
        self.assertEqual(scheduler.stopped, 0)
        self.assertEqual(scheduler.unregistered, 0)

if __name__ == "__main__":
    unittest.main()
