from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


LocalTransport = Literal["unix", "npipe"]


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
