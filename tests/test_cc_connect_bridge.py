from pathlib import Path
import json
import socket
import tempfile
import threading
import unittest

from agent_chat_session_sync.bridges.cc_connect import CCConnectBridge


class CCConnectBridgeTests(unittest.TestCase):
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
