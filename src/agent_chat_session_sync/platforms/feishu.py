from __future__ import annotations

import json
from pathlib import Path
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from ..errors import PlatformAPIError


# Feishu uses 232009 when a formerly valid chat has been dissolved.  Treat it
# like the other missing/unavailable-chat responses so the coordinator can
# invalidate the durable binding and recreate the per-session group.
STALE_CHAT_CODES = {230001, 230002, 232009, 99992356}


class FeishuPlatform:
    def __init__(self, app_id: str, app_secret: str, user_open_id: str):
        if not app_id or not app_secret or not user_open_id or user_open_id == "*":
            raise ValueError("Feishu app_id/app_secret and one concrete allow_from user are required")
        self.app_id = app_id
        self.app_secret = app_secret
        self.user_open_id = user_open_id
        self._token: str | None = None

    @classmethod
    def from_options(cls, options: dict[str, Any]) -> "FeishuPlatform":
        user = str(options.get("allow_from", "")).split(",", 1)[0].strip()
        return cls(str(options.get("app_id", "")), str(options.get("app_secret", "")), user)

    @staticmethod
    def _api_json(
        url: str, data: dict[str, Any] | None = None, token: str | None = None, method: str | None = None
    ) -> dict[str, Any]:
        body = None if data is None else json.dumps(data, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(
            url, data=body, headers=headers, method=method or ("POST" if data is not None else "GET")
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            text = exc.read().decode("utf-8", errors="replace")
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                raise PlatformAPIError(None, text[:500], exc.code) from exc
            raise PlatformAPIError(payload.get("code"), payload.get("msg", "unknown error"), exc.code) from exc
        if payload.get("code", 0) != 0:
            raise PlatformAPIError(payload.get("code"), payload.get("msg", "unknown error"))
        return payload

    def token(self) -> str:
        if self._token is None:
            payload = self._api_json(
                "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                {"app_id": self.app_id, "app_secret": self.app_secret},
            )
            self._token = str(payload.get("tenant_access_token", ""))
            if not self._token:
                raise RuntimeError("Feishu token response did not contain tenant_access_token")
        return self._token

    def create_session_chat(self, session_id: str, cwd: str, generation: int, title: str) -> str:
        payload = self._api_json(
            "https://open.feishu.cn/open-apis/im/v1/chats?user_id_type=open_id",
            {
                "name": title[:60],
                "description": f"本地 Agent 会话 {session_id}"[:100],
                "chat_mode": "group",
                "chat_type": "private",
                "user_id_list": [self.user_open_id],
                "owner_id": self.user_open_id,
                # Keep the legacy key namespace so migrations do not create a
                # duplicate group when Feishu still remembers an old request.
                "uuid": f"codex-{session_id}-{generation}",
            },
            self.token(),
        )
        chat_id = str(payload.get("data", {}).get("chat_id", ""))
        if not chat_id:
            raise RuntimeError("Feishu create chat response did not contain chat_id")
        return chat_id

    def session_key(self, chat_id: str) -> str:
        return f"feishu:{chat_id}:{self.user_open_id}"

    def validate_chat(self, chat_id: str) -> bool:
        if not chat_id:
            return False
        url = f"https://open.feishu.cn/open-apis/im/v1/chats/{urllib.parse.quote(chat_id)}"
        try:
            self._api_json(url, token=self.token())
            return True
        except PlatformAPIError as exc:
            if exc.code in STALE_CHAT_CODES:
                return False
            raise

    def send_message(self, chat_id: str, text: str, idempotency_key: str = "") -> str:
        if not text.strip():
            return ""
        max_chars = 28000
        chunks = [text[i : i + max_chars] for i in range(0, len(text), max_chars)]
        message_ids: list[str] = []
        for index, chunk in enumerate(chunks, 1):
            suffix = f"\n\n（{index}/{len(chunks)}）" if len(chunks) > 1 else ""
            uuid = ""
            if idempotency_key:
                import hashlib

                uuid = hashlib.sha256(f"{idempotency_key}:{index}".encode()).hexdigest()[:50]
            body = {
                "receive_id": chat_id,
                "msg_type": "text",
                "content": json.dumps({"text": chunk + suffix}, ensure_ascii=False),
            }
            if uuid:
                body["uuid"] = uuid
            payload = self._api_json(
                "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
                body,
                self.token(),
            )
            message_id = str(payload.get("data", {}).get("message_id", ""))
            if message_id:
                message_ids.append(message_id)
        return ",".join(message_ids)

    def rename_chat(self, chat_id: str, title: str) -> None:
        url = f"https://open.feishu.cn/open-apis/im/v1/chats/{urllib.parse.quote(chat_id)}"
        self._api_json(url, {"name": title[:60]}, self.token(), method="PUT")

    def get_message(self, message_id: str) -> dict[str, Any]:
        url = f"https://open.feishu.cn/open-apis/im/v1/messages/{urllib.parse.quote(message_id)}"
        return self._api_json(url, token=self.token()).get("data", {})

    def disband_chat(self, chat_id: str) -> None:
        url = f"https://open.feishu.cn/open-apis/im/v1/chats/{urllib.parse.quote(chat_id)}"
        self._api_json(url, token=self.token(), method="DELETE")
