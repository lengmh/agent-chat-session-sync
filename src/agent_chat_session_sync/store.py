from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
from typing import Any

from .models import Binding
from .security import ensure_private_directory, harden_private_file


class BindingStore:
    SCHEMA_VERSION = 3

    def __init__(self, path: Path):
        self.path = path
        self._state: dict[str, Any] | None = None

    def load(self) -> None:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            value = {"version": self.SCHEMA_VERSION, "sessions": {}}
        value.setdefault("sessions", {})
        value["version"] = self.SCHEMA_VERSION
        self._state = value

    @property
    def state(self) -> dict[str, Any]:
        if self._state is None:
            self.load()
        assert self._state is not None
        return self._state

    def get(self, session_id: str) -> Binding | None:
        raw = self.state["sessions"].get(session_id)
        return Binding.from_dict(raw) if raw else None

    def bind(self, session_id: str, binding: Binding) -> None:
        self.state["sessions"][session_id] = binding.to_dict()
        self.save()

    def invalidate(self, session_id: str, expected_chat_id: str) -> bool:
        current = self.state["sessions"].get(session_id)
        if not current or current.get("chat_id") != expected_chat_id:
            return False
        del self.state["sessions"][session_id]
        self.save()
        return True

    def save(self) -> None:
        ensure_private_directory(self.path.parent)
        fd, tmp_name = tempfile.mkstemp(prefix=self.path.name + ".", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(self.state, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            harden_private_file(Path(tmp_name))
            os.replace(tmp_name, self.path)
        finally:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass
