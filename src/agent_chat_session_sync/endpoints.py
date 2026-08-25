from __future__ import annotations

from dataclasses import dataclass
import hashlib
from typing import Literal


LocalTransport = Literal["unix", "npipe"]


def windows_default_local_endpoint(user_sid: str) -> "LocalEndpoint":
    normalized_sid = user_sid.strip().upper()
    if not normalized_sid:
        raise ValueError("current user SID is required")
    digest = hashlib.sha256(normalized_sid.encode("utf-8")).hexdigest()[:16]
    return LocalEndpoint("npipe", f"./pipe/cc-connect-api-{digest}")


@dataclass(frozen=True)
class LocalEndpoint:
    transport: LocalTransport
    address: str

    @classmethod
    def parse(cls, value: str) -> "LocalEndpoint":
        scheme, separator, address = value.partition("://")
        if separator != "://" or scheme not in {"unix", "npipe"}:
            raise ValueError(f"unsupported local endpoint transport: {scheme or '<missing>'}")
        if not address:
            raise ValueError("local endpoint address is required")
        return cls(scheme, address)  # type: ignore[arg-type]

    def __str__(self) -> str:
        return f"{self.transport}://{self.address}"
