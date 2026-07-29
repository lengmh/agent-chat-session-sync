from __future__ import annotations

import http.client
import json
from pathlib import Path
import socket
from typing import Any


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: Path):
        super().__init__("localhost", timeout=10)
        self.socket_path = str(socket_path)

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.socket_path)


class CCConnectBridge:
    def __init__(self, socket_path: Path):
        self.socket_path = socket_path

    def attach_agent_session(
        self, project: str, session_key: str, session_id: str, name: str, work_dir: str
    ) -> dict[str, Any]:
        connection = UnixHTTPConnection(self.socket_path)
        body = json.dumps(
            {
                "project": project,
                "session_key": session_key,
                "session_id": session_id,
                "session_name": name,
                "work_dir": work_dir,
            },
            ensure_ascii=False,
        )
        try:
            connection.request(
                "POST",
                "/sessions/bind-agent",
                body=body.encode("utf-8"),
                headers={"Content-Type": "application/json; charset=utf-8"},
            )
            response = connection.getresponse()
            text = response.read().decode("utf-8", errors="replace")
        finally:
            connection.close()
        if response.status != 200:
            raise RuntimeError(f"cc-connect bind HTTP {response.status}: {text[:500]}")
        return json.loads(text)

    def health_check(self) -> bool:
        return self.socket_path.exists()

    def supports_attach(self) -> bool:
        """Probe the extension without creating or changing a binding."""
        connection = UnixHTTPConnection(self.socket_path)
        try:
            connection.request("POST", "/sessions/bind-agent", body="{}", headers={"Content-Type": "application/json"})
            response = connection.getresponse()
            response.read()
            return response.status == 400
        except OSError:
            return False
        finally:
            connection.close()

    def capabilities(self) -> set[str]:
        connection = UnixHTTPConnection(self.socket_path)
        try:
            connection.request("GET", "/sessions/bind-agent")
            response = connection.getresponse()
            payload = response.read().decode("utf-8", errors="replace")
            if response.status != 200:
                return set()
            document = json.loads(payload)
            return {str(item) for item in document.get("capabilities", [])}
        except (OSError, ValueError, json.JSONDecodeError):
            return set()
        finally:
            connection.close()
