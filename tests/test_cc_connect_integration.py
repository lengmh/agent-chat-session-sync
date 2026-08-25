from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import textwrap
import unittest
import uuid

from agent_chat_session_sync.bridges.cc_connect import CCConnectBridge
from agent_chat_session_sync.endpoints import LocalEndpoint
from agent_chat_session_sync.permissions import local_endpoint_security_checks


@unittest.skipUnless(os.name == "nt", "Windows Named Pipe integration contract")
class PatchedCCConnectIntegrationTests(unittest.TestCase):
    def test_python_bridge_uses_real_patched_listener(self) -> None:
        source_value = os.environ.get("ACSS_CC_CONNECT_SOURCE", "")
        go_value = os.environ.get("ACSS_GO", "")
        temp_value = os.environ.get("ACSS_TEMP_DIR", "")
        if not source_value or not go_value or not temp_value:
            self.skipTest(
                "set ACSS_CC_CONNECT_SOURCE, ACSS_GO, and ACSS_TEMP_DIR for patched listener integration"
            )

        source = Path(source_value).resolve()
        go = Path(go_value).resolve()
        temp_root = Path(temp_value).resolve()
        self.assertTrue((source / "core" / "api_windows.go").is_file())
        self.assertTrue(go.is_file())
        temp_root.mkdir(parents=True, exist_ok=True)

        pipe_name = f"cc-connect-python-integration-{uuid.uuid4().hex}"
        endpoint = LocalEndpoint("npipe", f"./pipe/{pipe_name}")
        with tempfile.TemporaryDirectory(
            prefix="acss-cc-connect-helper-",
            dir=temp_root,
        ) as raw:
            helper = Path(raw)
            source_module = json.dumps(source.as_posix())
            (helper / "go.mod").write_text(
                textwrap.dedent(
                    f"""\
                    module acss-cc-connect-integration

                    go 1.25.0

                    require github.com/chenhg5/cc-connect v0.0.0

                    replace github.com/chenhg5/cc-connect => {source_module}
                    """
                ),
                encoding="utf-8",
            )
            (helper / "main.go").write_text(
                textwrap.dedent(
                    """\
                    package main

                    import (
                        "fmt"
                        "os"

                        "github.com/chenhg5/cc-connect/core"
                    )

                    func main() {
                        api, err := core.NewAPIServerWithEndpoint(os.TempDir(), os.Args[1])
                        if err != nil {
                            panic(err)
                        }
                        api.Start()
                        defer api.Stop()
                        fmt.Println("READY")
                        var stop [1]byte
                        _, _ = os.Stdin.Read(stop[:])
                    }
                    """
                ),
                encoding="utf-8",
            )
            process = subprocess.Popen(
                [str(go), "run", "-mod=mod", ".", str(endpoint)],
                cwd=helper,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            try:
                self.assertIsNotNone(process.stdout)
                ready = process.stdout.readline().strip()
                if ready != "READY":
                    process.wait(timeout=10)
                    stderr = process.stderr.read() if process.stderr is not None else ""
                    self.fail(
                        f"patched listener helper did not become ready: {ready!r}; {stderr}"
                    )
                self.assertTrue(
                    all(
                        check.okay
                        for check in local_endpoint_security_checks(
                            "cc-connect endpoint",
                            endpoint,
                        )
                    )
                )
                bridge = CCConnectBridge(endpoint)
                info = bridge.inspect()
                self.assertEqual(info.transport, "npipe")
                self.assertIn("local_endpoint_v2", info.capabilities)
                self.assertTrue(info.instance_id)
                with self.assertRaisesRegex(RuntimeError, "HTTP 404"):
                    bridge.attach_agent_session(
                        "missing-project",
                        "feishu:chat:user",
                        "native-session",
                        "中文集成",
                        str(helper),
                    )
            finally:
                if process.poll() is None:
                    try:
                        if process.stdin is not None:
                            process.stdin.write("\n")
                            process.stdin.flush()
                    except (BrokenPipeError, OSError):
                        pass
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
                if process.stdin is not None:
                    process.stdin.close()
                if process.stdout is not None:
                    process.stdout.close()
                if process.stderr is not None:
                    process.stderr.close()


if __name__ == "__main__":
    unittest.main()
