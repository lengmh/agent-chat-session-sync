from pathlib import Path
import json
import os
import socket
import tempfile
import threading
import time
import unittest
import uuid

from agent_chat_session_sync.bridges.cc_connect import CCConnectBridge
from agent_chat_session_sync.endpoints import LocalEndpoint


class CCConnectBridgeTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows Named Pipe transport contract")
    def test_named_pipe_read_times_out(self) -> None:
        import win32file
        import win32pipe

        pipe_name = f"cc-connect-python-timeout-{uuid.uuid4().hex}"
        pipe_path = rf"\\.\pipe\{pipe_name}"
        ready = threading.Event()

        def serve() -> None:
            handle = win32pipe.CreateNamedPipe(
                pipe_path,
                win32pipe.PIPE_ACCESS_DUPLEX,
                win32pipe.PIPE_TYPE_BYTE | win32pipe.PIPE_READMODE_BYTE | win32pipe.PIPE_WAIT,
                1,
                65536,
                65536,
                0,
                None,
            )
            ready.set()
            try:
                win32pipe.ConnectNamedPipe(handle, None)
                time.sleep(0.2)
            finally:
                win32file.CloseHandle(handle)

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        self.assertTrue(ready.wait(timeout=2))
        bridge = CCConnectBridge(
            LocalEndpoint("npipe", f"./pipe/{pipe_name}"),
            timeout=0.05,
        )
        started = time.monotonic()
        with self.assertRaises(TimeoutError):
            bridge.inspect()
        self.assertLess(time.monotonic() - started, 1)
        thread.join(timeout=1)
        self.assertFalse(thread.is_alive())

    @unittest.skipUnless(os.name == "nt", "Windows Named Pipe transport contract")
    def test_inspect_uses_named_pipe_http_transport(self) -> None:
        import win32file
        import win32pipe

        pipe_name = f"cc-connect-python-test-{uuid.uuid4().hex}"
        pipe_path = rf"\\.\pipe\{pipe_name}"
        ready = threading.Event()

        def serve() -> None:
            handle = win32pipe.CreateNamedPipe(
                pipe_path,
                win32pipe.PIPE_ACCESS_DUPLEX,
                win32pipe.PIPE_TYPE_BYTE | win32pipe.PIPE_READMODE_BYTE | win32pipe.PIPE_WAIT,
                1,
                65536,
                65536,
                0,
                None,
            )
            ready.set()
            try:
                win32pipe.ConnectNamedPipe(handle, None)
                request = b""
                while b"\r\n\r\n" not in request:
                    _status, chunk = win32file.ReadFile(handle, 4096)
                    request += chunk
                self.assertTrue(request.startswith(b"GET /sessions/bind-agent HTTP/1.1\r\n"))
                response = json.dumps(
                    {
                        "capabilities": ["attach_agent_session", "local_endpoint_v2"],
                        "transport": "npipe",
                        "instance_id": "instance-pipe",
                    }
                ).encode()
                time.sleep(0.05)
                win32file.WriteFile(
                    handle,
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
                    + str(len(response)).encode()
                    + b"\r\n\r\n"
                    + response,
                )
                win32file.FlushFileBuffers(handle)
            finally:
                win32file.CloseHandle(handle)

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        self.assertTrue(ready.wait(timeout=2))
        info = CCConnectBridge(LocalEndpoint("npipe", f"./pipe/{pipe_name}")).inspect()
        thread.join(timeout=2)

        self.assertEqual(info.transport, "npipe")
        self.assertEqual(info.instance_id, "instance-pipe")

    @unittest.skipUnless(hasattr(socket, "AF_UNIX"), "Unix socket transport contract")
    def test_inspect_returns_local_endpoint_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "api.sock"
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(str(path))
            server.listen(1)

            def serve() -> None:
                connection, _ = server.accept()
                while b"\r\n\r\n" not in connection.recv(4096):
                    pass
                response = json.dumps(
                    {
                        "capabilities": ["attach_agent_session", "local_endpoint_v2"],
                        "transport": "unix",
                        "instance_id": "instance-1",
                    }
                ).encode()
                connection.sendall(
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
                    + str(len(response)).encode()
                    + b"\r\n\r\n"
                    + response
                )
                connection.close()

            thread = threading.Thread(target=serve, daemon=True)
            thread.start()
            try:
                info = CCConnectBridge(LocalEndpoint("unix", str(path))).inspect()
            finally:
                thread.join(timeout=2)
                server.close()

            self.assertEqual(info.capabilities, frozenset({"attach_agent_session", "local_endpoint_v2"}))
            self.assertEqual(info.transport, "unix")
            self.assertEqual(info.instance_id, "instance-1")

    @unittest.skipUnless(hasattr(socket, "AF_UNIX"), "Unix socket transport contract")
    def test_unicode_session_name_is_sent_as_utf8(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "api.sock"
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(str(path))
            server.listen(1)
            received: dict = {}

            def serve() -> None:
                connection, _ = server.accept()
                payload = b""
                while b"\r\n\r\n" not in payload:
                    payload += connection.recv(4096)
                head, body = payload.split(b"\r\n\r\n", 1)
                length = int(next(line.split(b":", 1)[1] for line in head.split(b"\r\n") if line.lower().startswith(b"content-length:")))
                while len(body) < length:
                    body += connection.recv(4096)
                received.update(json.loads(body[:length].decode("utf-8")))
                response = b'{"status":"ok"}'
                connection.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: " + str(len(response)).encode() + b"\r\n\r\n" + response)
                connection.close()

            thread = threading.Thread(target=serve)
            thread.start()
            try:
                CCConnectBridge(path).attach_agent_session("p", "feishu:chat:user", "id", "中文任务名", "/work")
            finally:
                thread.join(timeout=2)
                server.close()
            self.assertEqual(received["session_name"], "中文任务名")


if __name__ == "__main__":
    unittest.main()
