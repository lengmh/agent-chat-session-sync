from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import sqlite3
import time
from typing import Any, Iterator

from .models import Binding


EVENT_STATES = {
    "received",
    "resolving_session",
    "waiting_confirmation",
    "resolved",
    "binding_chat",
    "sending",
    "delivered",
    "dead_letter",
}


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def receipt_id(raw: dict[str, Any], bridge_originated: bool = False, agent_type: str = "codex") -> str:
    envelope: dict[str, Any] = {"raw": raw, "bridge_originated": bridge_originated}
    if agent_type != "codex":
        envelope["agent_type"] = agent_type
    return sha256_text(canonical_json(envelope))


def stable_event_id(
    rollout_id: str, event_name: str, turn_id: str, content: str, agent_type: str = "codex"
) -> str:
    content_hash = sha256_text(content)
    discriminator = turn_id.strip() or content_hash
    parts = (rollout_id.lower(), event_name, discriminator, content_hash)
    if agent_type != "codex":
        parts = (agent_type, *parts)
    return sha256_text("\x00".join(parts))


@dataclass(frozen=True)
class QueuedEvent:
    id: int
    receipt_id: str
    state: str
    raw: dict[str, Any]
    bridge_originated: bool
    agent_type: str
    received_at: float
    attempts: int
    rollout_id: str
    rollout_path: str
    resolution_method: str
    candidates: tuple[dict[str, Any], ...]
    stable_event_id: str
    last_error: str


@dataclass(frozen=True)
class OutboxRecord:
    event_id: str
    inbox_id: int
    rollout_id: str
    payload: str
    status: str
    attempts: int
    platform_message_id: str


class EventDatabase:
    SCHEMA_VERSION = 2

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=10000")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self.connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS inbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    receipt_id TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL,
                    raw_json TEXT NOT NULL,
                    bridge_originated INTEGER NOT NULL DEFAULT 0,
                    agent_type TEXT NOT NULL DEFAULT 'codex',
                    received_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    next_attempt_at REAL NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    rollout_id TEXT NOT NULL DEFAULT '',
                    rollout_path TEXT NOT NULL DEFAULT '',
                    resolution_method TEXT NOT NULL DEFAULT '',
                    candidates_json TEXT NOT NULL DEFAULT '[]',
                    stable_event_id TEXT NOT NULL DEFAULT '',
                    last_error TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS inbox_due_idx
                    ON inbox(state, next_attempt_at, id);
                CREATE TABLE IF NOT EXISTS outbox (
                    event_id TEXT PRIMARY KEY,
                    inbox_id INTEGER NOT NULL REFERENCES inbox(id),
                    rollout_id TEXT NOT NULL,
                    event_name TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at REAL NOT NULL,
                    platform_message_id TEXT NOT NULL DEFAULT '',
                    last_error TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS outbox_due_idx
                    ON outbox(status, next_attempt_at);
                CREATE TABLE IF NOT EXISTS bindings (
                    rollout_id TEXT PRIMARY KEY,
                    chat_id TEXT NOT NULL,
                    session_key TEXT NOT NULL,
                    project TEXT NOT NULL,
                    cwd TEXT NOT NULL,
                    generation INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    title TEXT NOT NULL DEFAULT '',
                    updated_at REAL NOT NULL
                );
                """
            )
            columns = {str(row["name"]) for row in db.execute("PRAGMA table_info(inbox)")}
            if "agent_type" not in columns:
                db.execute("ALTER TABLE inbox ADD COLUMN agent_type TEXT NOT NULL DEFAULT 'codex'")
            db.execute(
                "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(self.SCHEMA_VERSION),),
            )
        self.path.chmod(0o600)

    def enqueue(
        self,
        raw: dict[str, Any],
        bridge_originated: bool = False,
        now: float | None = None,
        agent_type: str = "codex",
    ) -> tuple[int, bool]:
        timestamp = time.time() if now is None else now
        rid = receipt_id(raw, bridge_originated, agent_type)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT id FROM inbox WHERE receipt_id=?", (rid,)).fetchone()
            if existing:
                db.execute("COMMIT")
                return int(existing["id"]), False
            cursor = db.execute(
                "INSERT INTO inbox(receipt_id,state,raw_json,bridge_originated,agent_type,received_at,updated_at,next_attempt_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (rid, "received", canonical_json(raw), int(bridge_originated), agent_type, timestamp, timestamp, timestamp),
            )
            event_id = int(cursor.lastrowid)
            db.execute("COMMIT")
            return event_id, True

    def claim_due(self, now: float | None = None) -> QueuedEvent | None:
        timestamp = time.time() if now is None else now
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM inbox WHERE state NOT IN ('delivered','dead_letter') "
                "AND next_attempt_at<=? ORDER BY id LIMIT 1",
                (timestamp,),
            ).fetchone()
            if row is None:
                db.execute("COMMIT")
                return None
            db.execute(
                "UPDATE inbox SET state='resolving_session',attempts=attempts+1,updated_at=? WHERE id=?",
                (timestamp, row["id"]),
            )
            db.execute("COMMIT")
            values = dict(row)
            values["state"] = "resolving_session"
            values["attempts"] = int(values["attempts"]) + 1
            return self._event(values)

    def get_event(self, event_id: int) -> QueuedEvent | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM inbox WHERE id=?", (event_id,)).fetchone()
        return self._event(row) if row else None

    def list_events(self, limit: int = 50) -> list[QueuedEvent]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM inbox ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self._event(row) for row in rows]

    def events_for_rollout(self, rollout_id: str, agent_type: str = "") -> list[QueuedEvent]:
        with self.connect() as db:
            if agent_type:
                rows = db.execute(
                    "SELECT * FROM inbox WHERE rollout_id=? AND agent_type=? ORDER BY id",
                    (rollout_id, agent_type),
                ).fetchall()
            else:
                rows = db.execute("SELECT * FROM inbox WHERE rollout_id=? ORDER BY id", (rollout_id,)).fetchall()
        return [self._event(row) for row in rows]

    def outbox_for_rollout(self, rollout_id: str) -> list[OutboxRecord]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM outbox WHERE rollout_id=? ORDER BY created_at", (rollout_id,)).fetchall()
        return [self._outbox(row) for row in rows]

    @staticmethod
    def _event(row: sqlite3.Row | dict[str, Any]) -> QueuedEvent:
        return QueuedEvent(
            id=int(row["id"]),
            receipt_id=str(row["receipt_id"]),
            state=str(row["state"]),
            raw=json.loads(str(row["raw_json"])),
            bridge_originated=bool(row["bridge_originated"]),
            agent_type=str(row["agent_type"]),
            received_at=float(row["received_at"]),
            attempts=int(row["attempts"]),
            rollout_id=str(row["rollout_id"]),
            rollout_path=str(row["rollout_path"]),
            resolution_method=str(row["resolution_method"]),
            candidates=tuple(json.loads(str(row["candidates_json"]))),
            stable_event_id=str(row["stable_event_id"]),
            last_error=str(row["last_error"]),
        )

    def transition(self, event_id: int, state: str, **fields: Any) -> None:
        if state not in EVENT_STATES:
            raise ValueError(f"invalid event state: {state}")
        allowed = {
            "rollout_id",
            "rollout_path",
            "resolution_method",
            "stable_event_id",
            "last_error",
            "next_attempt_at",
        }
        assignments = ["state=?", "updated_at=?"]
        values: list[Any] = [state, time.time()]
        for key, value in fields.items():
            if key == "candidates":
                assignments.append("candidates_json=?")
                values.append(canonical_json(value))
            elif key in allowed:
                assignments.append(f"{key}=?")
                values.append(value)
            else:
                raise ValueError(f"unsupported inbox field: {key}")
        values.append(event_id)
        with self.connect() as db:
            db.execute(f"UPDATE inbox SET {','.join(assignments)} WHERE id=?", values)

    def retry(self, event_id: int, state: str, error: str, delay: float, candidates: Any = ()) -> None:
        self.transition(
            event_id,
            state,
            last_error=error[:2000],
            candidates=candidates,
            next_attempt_at=time.time() + max(0.0, delay),
        )

    def force_resolution(self, event_id: int, rollout_id: str, rollout_path: str) -> None:
        self.transition(
            event_id,
            "resolved",
            rollout_id=rollout_id,
            rollout_path=rollout_path,
            resolution_method="manual_confirmation",
            candidates=(),
            last_error="",
            next_attempt_at=time.time(),
        )

    def retry_now(self, event_id: int) -> bool:
        with self.connect() as db:
            row = db.execute("SELECT stable_event_id,state FROM inbox WHERE id=?", (event_id,)).fetchone()
            if row is None or row["state"] == "delivered":
                return False
            db.execute(
                "UPDATE inbox SET next_attempt_at=0,last_error='',updated_at=? WHERE id=?",
                (time.time(), event_id),
            )
            if row["stable_event_id"]:
                db.execute(
                    "UPDATE outbox SET status='pending',next_attempt_at=0,updated_at=? "
                    "WHERE event_id=? AND status!='delivered'",
                    (time.time(), row["stable_event_id"]),
                )
        return True

    def ensure_outbox(
        self, event_id: str, inbox_id: int, rollout_id: str, event_name: str, payload: str
    ) -> OutboxRecord:
        now = time.time()
        with self.connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO outbox(event_id,inbox_id,rollout_id,event_name,content_hash,payload,status,next_attempt_at,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,'pending',?,?,?)",
                (event_id, inbox_id, rollout_id, event_name, sha256_text(payload), payload, now, now, now),
            )
            row = db.execute("SELECT * FROM outbox WHERE event_id=?", (event_id,)).fetchone()
        assert row is not None
        return self._outbox(row)

    def get_outbox(self, event_id: str) -> OutboxRecord | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM outbox WHERE event_id=?", (event_id,)).fetchone()
        return self._outbox(row) if row else None

    @staticmethod
    def _outbox(row: sqlite3.Row) -> OutboxRecord:
        return OutboxRecord(
            event_id=str(row["event_id"]),
            inbox_id=int(row["inbox_id"]),
            rollout_id=str(row["rollout_id"]),
            payload=str(row["payload"]),
            status=str(row["status"]),
            attempts=int(row["attempts"]),
            platform_message_id=str(row["platform_message_id"]),
        )

    def mark_outbox_sending(self, event_id: str) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE outbox SET status='sending',attempts=attempts+1,updated_at=? WHERE event_id=? AND status!='delivered'",
                (time.time(), event_id),
            )

    def mark_outbox_delivered(self, event_id: str, platform_message_id: str) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE outbox SET status='delivered',platform_message_id=?,last_error='',updated_at=? WHERE event_id=?",
                (platform_message_id, time.time(), event_id),
            )

    def mark_outbox_retry(self, event_id: str, error: str, delay: float) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE outbox SET status='pending',last_error=?,next_attempt_at=?,updated_at=? WHERE event_id=?",
                (error[:2000], time.time() + delay, time.time(), event_id),
            )

    def get_binding(self, rollout_id: str) -> Binding | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM bindings WHERE rollout_id=?", (rollout_id,)).fetchone()
        if not row:
            return None
        return Binding(
            chat_id=str(row["chat_id"]),
            session_key=str(row["session_key"]),
            project=str(row["project"]),
            cwd=str(row["cwd"]),
            generation=int(row["generation"]),
            created_at=str(row["created_at"]),
            title=str(row["title"]),
        )

    def put_binding(self, rollout_id: str, binding: Binding) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT INTO bindings(rollout_id,chat_id,session_key,project,cwd,generation,created_at,title,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(rollout_id) DO UPDATE SET "
                "chat_id=excluded.chat_id,session_key=excluded.session_key,project=excluded.project,cwd=excluded.cwd,"
                "generation=excluded.generation,created_at=excluded.created_at,title=excluded.title,updated_at=excluded.updated_at",
                (
                    rollout_id,
                    binding.chat_id,
                    binding.session_key,
                    binding.project,
                    binding.cwd,
                    binding.generation,
                    binding.created_at,
                    binding.title,
                    time.time(),
                ),
            )

    def list_bindings(self) -> list[tuple[str, Binding]]:
        """Return a stable snapshot used to rebuild cc-connect's in-memory routes."""
        with self.connect() as db:
            rows = db.execute("SELECT * FROM bindings ORDER BY rollout_id").fetchall()
        return [
            (
                str(row["rollout_id"]),
                Binding(
                    chat_id=str(row["chat_id"]),
                    session_key=str(row["session_key"]),
                    project=str(row["project"]),
                    cwd=str(row["cwd"]),
                    generation=int(row["generation"]),
                    created_at=str(row["created_at"]),
                    title=str(row["title"]),
                ),
            )
            for row in rows
        ]

    def invalidate_binding(self, rollout_id: str, expected_chat_id: str) -> bool:
        with self.connect() as db:
            cursor = db.execute(
                "DELETE FROM bindings WHERE rollout_id=? AND chat_id=?", (rollout_id, expected_chat_id)
            )
        return cursor.rowcount == 1

    def import_legacy_bindings(self, state_path: Path) -> int:
        with self.connect() as db:
            done = db.execute("SELECT value FROM meta WHERE key='legacy_state_imported'").fetchone()
        if done:
            return 0
        imported = 0
        try:
            document = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            document = {"sessions": {}}
        for rollout_id, raw in document.get("sessions", {}).items():
            self.put_binding(str(rollout_id), Binding.from_dict(raw))
            imported += 1
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('legacy_state_imported',?)", (str(time.time()),))
        return imported

    def stats(self) -> dict[str, int]:
        with self.connect() as db:
            states = {str(row["state"]): int(row["count"]) for row in db.execute(
                "SELECT state,COUNT(*) AS count FROM inbox GROUP BY state"
            )}
            pending_outbox = int(db.execute(
                "SELECT COUNT(*) FROM outbox WHERE status!='delivered'"
            ).fetchone()[0])
            bindings = int(db.execute("SELECT COUNT(*) FROM bindings").fetchone()[0])
        states["pending_outbox"] = pending_outbox
        states["bindings"] = bindings
        return states


class SQLiteBindingStore:
    def __init__(self, database: EventDatabase):
        self.database = database

    def get(self, session_id: str) -> Binding | None:
        return self.database.get_binding(session_id)

    def bind(self, session_id: str, binding: Binding) -> None:
        self.database.put_binding(session_id, binding)

    def invalidate(self, session_id: str, expected_chat_id: str) -> bool:
        return self.database.invalidate_binding(session_id, expected_chat_id)
