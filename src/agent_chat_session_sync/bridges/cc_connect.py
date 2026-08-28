from __future__ import annotations

import http.client
import io
import json
import os
from dataclasses import dataclass
from pathlib import Path
import socket
from typing import Any

from ..endpoints import LocalEndpoint, LocalTransport

if os.name == "nt":
    import pywintypes
    import win32con
    import win32event
    import win32file
    import win32pipe
    import winerror


@dataclass(frozen=True)
class BridgeInfo:
    capabilities: frozenset[str]
    transport: LocalTransport
    instance_id: str


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: Path, timeout: float):
        super().__init__("localhost", timeout=timeout)
        self.socket_path = str(socket_path)

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.socket_path)


class _NamedPipeRawIO(io.RawIOBase):
    def __init__(self, pipe: "NamedPipeSocket"):
        super().__init__()
        self.pipe = pipe

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int:
        data = self.pipe.recv(len(buffer))
        buffer[: len(data)] = data
        return len(data)


class NamedPipeSocket:
    def __init__(self, address: str, timeout: float):
        if os.name != "nt":
            raise OSError("Windows Named Pipe transport is unavailable on this platform")
        prefix = "./pipe/"
        if not address.startswith(prefix) or address == prefix:
            raise ValueError("invalid Windows Named Pipe endpoint")
        self.pipe_path = rf"\\.\pipe\{address.removeprefix(prefix)}"
        self.timeout = timeout
        win32pipe.WaitNamedPipe(self.pipe_path, self._timeout_ms())
        self.handle = win32file.CreateFile(
            self.pipe_path,
            win32con.GENERIC_READ | win32con.GENERIC_WRITE,
            0,
            None,
            win32con.OPEN_EXISTING,
            win32file.FILE_FLAG_OVERLAPPED,
            None,
        )
        try:
            win32pipe.SetNamedPipeHandleState(
                self.handle,
                win32pipe.PIPE_READMODE_BYTE,
                None,
                None,
            )
        except Exception:
            win32file.CloseHandle(self.handle)
            self.handle = None
            raise

    def _timeout_ms(self) -> int:
        return max(1, int(float(self.timeout) * 1000))

    def _complete(self, overlapped: Any) -> int:
        result = win32event.WaitForSingleObject(overlapped.hEvent, self._timeout_ms())
        if result == win32event.WAIT_TIMEOUT:
            try:
                win32file.CancelIo(self.handle)
            except pywintypes.error as exc:
                if exc.winerror != winerror.ERROR_NOT_FOUND:
                    raise
            cancelled = win32event.WaitForSingleObject(
                overlapped.hEvent,
                win32event.INFINITE,
            )
            if cancelled != win32event.WAIT_OBJECT_0:
                raise OSError(f"unexpected Named Pipe cancellation result: {cancelled}")
            try:
                win32file.GetOverlappedResult(self.handle, overlapped, False)
            except pywintypes.error as exc:
                if exc.winerror != winerror.ERROR_OPERATION_ABORTED:
                    raise
            raise TimeoutError(f"timed out waiting for Named Pipe I/O: {self.pipe_path}")
        if result != win32event.WAIT_OBJECT_0:
            raise OSError(f"unexpected Named Pipe wait result: {result}")
        return int(win32file.GetOverlappedResult(self.handle, overlapped, False))

    def sendall(self, data: bytes) -> None:
        offset = 0
        while offset < len(data):
            chunk = data[offset:]
            overlapped = pywintypes.OVERLAPPED()
            overlapped.hEvent = win32event.CreateEvent(None, True, False, None)
            try:
                win32file.WriteFile(self.handle, chunk, overlapped)
                written = self._complete(overlapped)
            finally:
                overlapped.hEvent.Close()
            if written <= 0:
                raise BrokenPipeError(f"Named Pipe write returned {written} bytes")
            offset += written

    def recv(self, size: int) -> bytes:
        overlapped = pywintypes.OVERLAPPED()
        overlapped.hEvent = win32event.CreateEvent(None, True, False, None)
        buffer = win32file.AllocateReadBuffer(size)
        try:
            try:
                win32file.ReadFile(self.handle, buffer, overlapped)
                count = self._complete(overlapped)
                return bytes(buffer[:count])
            except pywintypes.error as exc:
                if exc.winerror in {winerror.ERROR_BROKEN_PIPE, winerror.ERROR_PIPE_NOT_CONNECTED}:
                    return b""
                raise
        finally:
            overlapped.hEvent.Close()

    def makefile(self, mode: str) -> io.BufferedReader:
        if mode != "rb":
            raise ValueError(f"unsupported Named Pipe makefile mode: {mode}")
        return io.BufferedReader(_NamedPipeRawIO(self))

    def close(self) -> None:
        handle = getattr(self, "handle", None)
        if handle is not None:
            self.handle = None
            win32file.CloseHandle(handle)


class NamedPipeHTTPConnection(http.client.HTTPConnection):
    def __init__(self, address: str, timeout: float):
        super().__init__("localhost", timeout=timeout)
        self.address = address

    def connect(self) -> None:
        self.sock = NamedPipeSocket(self.address, self.timeout)


class CCConnectBridge:
    def __init__(self, endpoint: LocalEndpoint | Path, timeout: float = 10):
        self.endpoint = (
            endpoint if isinstance(endpoint, LocalEndpoint) else LocalEndpoint("unix", str(endpoint))
        )
        self.timeout = timeout

    def _connection(self) -> http.client.HTTPConnection:
        if self.endpoint.transport == "unix":
            return UnixHTTPConnection(Path(self.endpoint.address), self.timeout)
        return NamedPipeHTTPConnection(self.endpoint.address, self.timeout)

    def _request(
        self,
        method: str,
        path: str,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, str]:
        connection = self._connection()
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, response.read().decode("utf-8", errors="replace")
        finally:
            connection.close()

    def attach_agent_session(
        self, project: str, session_key: str, session_id: str, name: str, work_dir: str
    ) -> dict[str, Any]:
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
        status, text = self._request(
            "POST",
            "/sessions/bind-agent",
            body=body.encode("utf-8"),
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        if status != 200:
            raise RuntimeError(f"cc-connect bind HTTP {status}: {text[:500]}")
        return json.loads(text)

    def inspect(self) -> BridgeInfo:
        status, payload = self._request("GET", "/sessions/bind-agent")
        if status != 200:
            raise RuntimeError(f"cc-connect inspect HTTP {status}: {payload[:500]}")
        document = json.loads(payload)
        capabilities = frozenset(str(item) for item in document.get("capabilities", []))
        transport = str(document.get("transport", ""))
        instance_id = str(document.get("instance_id", "")).strip()
        if transport not in {"unix", "npipe"}:
            raise ValueError("cc-connect response did not contain a valid transport")
        if transport != self.endpoint.transport:
            raise ValueError(
                f"cc-connect transport mismatch: expected {self.endpoint.transport}, got {transport}"
            )
        if not instance_id:
            raise ValueError("cc-connect response did not contain instance_id")
        return BridgeInfo(
            capabilities=capabilities,
            transport=transport,  # type: ignore[arg-type]
            instance_id=instance_id,
        )
