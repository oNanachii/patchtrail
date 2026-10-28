"""Small SQLite store for runs, optimistic state changes and ordered events."""

from contextlib import contextmanager
from datetime import datetime, timezone
from functools import wraps
import json
from pathlib import Path
import sqlite3
import threading
import uuid


class StoreConflict(RuntimeError):
    """The requested action disagrees with persisted state or an idempotency key."""


class RunNotFound(LookupError):
    """The requested run does not exist."""


TRANSITIONS = {
    "queued": {"diagnosing", "failed", "cancelled"},
    "diagnosing": {"awaiting_approval", "failed", "cancelled", "interrupted"},
    "awaiting_approval": {"verifying", "failed", "cancelled"},
    "verifying": {"succeeded", "failed", "cancelled", "interrupted"},
    "interrupted": {"queued", "verifying", "failed", "cancelled"},
    "succeeded": set(), "failed": set(), "cancelled": set(),
}
ACTIVE_STATES = {"queued", "diagnosing", "awaiting_approval", "verifying"}
JSON_FIELDS = {"limits", "checkpoint", "proposal", "result"}
UPDATE_FIELDS = {"checkpoint", "proposal", "result", "stop_reason"}
RUN_COLUMNS = (
    "id, task_id, mode, state, version, limits, checkpoint, proposal, result, "
    "stop_reason, created_at, updated_at"
)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _run(row):
    result = dict(row)
    for field in JSON_FIELDS:
        result[field] = json.loads(result[field]) if result[field] is not None else None
    return result


def _event(row):
    result = dict(row)
    result["payload"] = json.loads(result["payload"])
    return result


def _serialized_operation(operation):
    """Keep this instance's short-lived database connections from overlapping.

    This is an in-process boundary, not coordination across Store instances or
    processes. SQLite's existing busy timeout still governs external contention.
    """
    @wraps(operation)
    def serialized(self, *args, **kwargs):
        with self._operation_lock:
            return operation(self, *args, **kwargs)
    return serialized


class Store:
    def __init__(self, database: Path):
        self._operation_lock = threading.RLock()
        self.database = Path(database).resolve()
        self.database.parent.mkdir(parents=True, exist_ok=True)
        connection = self._connect()
        try:
            connection.executescript("""
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    request_key TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 0,
                    limits TEXT NOT NULL,
                    checkpoint TEXT NOT NULL DEFAULT '{}',
                    proposal TEXT,
                    result TEXT,
                    stop_reason TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL REFERENCES runs(id),
                    kind TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    call_id TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS events_by_run ON events(run_id, id);
                CREATE UNIQUE INDEX IF NOT EXISTS event_calls
                    ON events(run_id, kind, call_id) WHERE call_id IS NOT NULL;
                COMMIT;
            """)
        finally:
            connection.close()

    def _connect(self):
        connection = sqlite3.connect(self.database, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 10000")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @contextmanager
    def _transaction(self):
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _find(connection, run_id):
        row = connection.execute(f"SELECT {RUN_COLUMNS} FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise RunNotFound(run_id)
        return row

    @staticmethod
    def _insert_event(connection, run_id, kind, payload, call_id=None):
        cursor = connection.execute(
            "INSERT INTO events(run_id, kind, payload, call_id, created_at) VALUES (?, ?, ?, ?, ?)",
            (run_id, kind, _json(payload), call_id, _now()),
        )
        return connection.execute("SELECT * FROM events WHERE id = ?", (cursor.lastrowid,)).fetchone()

    @_serialized_operation
    def create_run(self, task_id: str, mode: str, request_key: str, limits: dict) -> dict:
        if not all(isinstance(value, str) and value for value in (task_id, mode, request_key)):
            raise ValueError("Task, mode and request key must be non-empty strings")
        if not isinstance(limits, dict):
            raise ValueError("Limits must be an object")
        encoded_limits = _json(limits)
        with self._transaction() as connection:
            previous = connection.execute("SELECT * FROM runs WHERE request_key = ?", (request_key,)).fetchone()
            if previous is not None:
                if (previous["task_id"], previous["mode"], previous["limits"]) != (task_id, mode, encoded_limits):
                    raise StoreConflict("Idempotency key was already used for a different request")
                row = _run(self._find(connection, previous["id"]))
                return {**row, "created": False}
            run_id, timestamp = uuid.uuid4().hex, _now()
            connection.execute(
                "INSERT INTO runs(id, task_id, mode, request_key, state, limits, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 'queued', ?, ?, ?)",
                (run_id, task_id, mode, request_key, encoded_limits, timestamp, timestamp),
            )
            return {**_run(self._find(connection, run_id)), "created": True}

    @_serialized_operation
    def get_run(self, run_id: str) -> dict:
        connection = self._connect()
        try:
            return _run(self._find(connection, run_id))
        finally:
            connection.close()

    @_serialized_operation
    def find_request(self, request_key: str) -> dict | None:
        """Read a prior request before checking capacity or external availability."""
        if not isinstance(request_key, str) or not request_key:
            raise ValueError("Request key must be a non-empty string")
        connection = self._connect()
        try:
            row = connection.execute(
                f"SELECT {RUN_COLUMNS} FROM runs WHERE request_key = ?", (request_key,)
            ).fetchone()
            return _run(row) if row is not None else None
        finally:
            connection.close()

    @_serialized_operation
    def list_runs(self, limit: int = 30) -> list[dict]:
        if type(limit) is not int or limit < 1:
            raise ValueError("Limit must be a positive integer")
        connection = self._connect()
        try:
            return [_run(row) for row in connection.execute(
                f"SELECT {RUN_COLUMNS} FROM runs ORDER BY created_at DESC, id DESC LIMIT ?", (min(limit, 100),)
            )]
        finally:
            connection.close()

    @_serialized_operation
    def transition(self, run_id: str, expected_states: set[str], state: str,
                   updates: dict | None = None, expected_version: int | None = None) -> dict:
        if state not in TRANSITIONS:
            raise ValueError("Unknown run state")
        if not isinstance(expected_states, (set, frozenset)):
            raise ValueError("Expected states must be a set")
        if expected_version is not None and (type(expected_version) is not int or expected_version < 0):
            raise ValueError("Expected version must be a non-negative integer")
        updates = {} if updates is None else dict(updates)
        if set(updates) - UPDATE_FIELDS:
            raise ValueError("Unsupported run update field")
        for field in ("checkpoint", "proposal", "result"):
            if field in updates and not isinstance(updates[field], dict):
                if field == "checkpoint" or updates[field] is not None:
                    raise ValueError(f"{field} must be an object" + (" or null" if field != "checkpoint" else ""))
        if "stop_reason" in updates and updates["stop_reason"] is not None and not isinstance(updates["stop_reason"], str):
            raise ValueError("Stop reason must be a string or null")
        values = {field: _json(value) if field in JSON_FIELDS and value is not None else value
                  for field, value in updates.items()}
        with self._transaction() as connection:
            previous = self._find(connection, run_id)
            if previous["state"] not in expected_states:
                raise StoreConflict("Run state changed")
            if expected_version is not None and previous["version"] != expected_version:
                raise StoreConflict("Run version changed")
            if previous["state"] == state:
                if state not in ACTIVE_STATES or set(updates) != {"checkpoint"}:
                    raise StoreConflict("Only active checkpoint updates may keep the same state")
            elif state not in TRANSITIONS[previous["state"]]:
                raise StoreConflict(f"Unsupported transition: {previous['state']} -> {state}")
            next_version = previous["version"] + 1
            fields = ["state = ?", "version = ?", "updated_at = ?"]
            parameters = [state, next_version, _now()]
            for field, value in values.items():
                fields.append(field + " = ?")
                parameters.append(value)
            connection.execute("UPDATE runs SET " + ", ".join(fields) + " WHERE id = ?", (*parameters, run_id))
            self._insert_event(connection, run_id, "state_changed",
                               {"from": previous["state"], "to": state, "version": next_version})
            return _run(self._find(connection, run_id))

    @_serialized_operation
    def append_event(self, run_id: str, kind: str, payload: dict, call_id: str | None = None) -> dict:
        if not isinstance(kind, str) or not kind or not isinstance(payload, dict):
            raise ValueError("Event kind must be non-empty and payload must be an object")
        if call_id is not None and (not isinstance(call_id, str) or not call_id):
            raise ValueError("Call ID must be a non-empty string when provided")
        encoded = _json(payload)
        with self._transaction() as connection:
            self._find(connection, run_id)
            if call_id is not None:
                previous = connection.execute(
                    "SELECT * FROM events WHERE run_id = ? AND kind = ? AND call_id = ?",
                    (run_id, kind, call_id),
                ).fetchone()
                if previous is not None:
                    if previous["payload"] != encoded:
                        raise StoreConflict("Call ID was already used with different event content")
                    return _event(previous)
            return _event(self._insert_event(connection, run_id, kind, payload, call_id))

    @_serialized_operation
    def events(self, run_id: str, after: int = 0) -> list[dict]:
        if type(after) is not int or after < 0:
            raise ValueError("Event cursor must be a non-negative integer")
        connection = self._connect()
        try:
            self._find(connection, run_id)
            return [_event(row) for row in connection.execute(
                "SELECT * FROM events WHERE run_id = ? AND id > ? ORDER BY id LIMIT 1000", (run_id, after)
            )]
        finally:
            connection.close()

    @_serialized_operation
    def recover_active(self) -> list[str]:
        with self._transaction() as connection:
            rows = connection.execute(
                "SELECT id, state, version FROM runs WHERE state IN ('diagnosing', 'verifying') "
                "ORDER BY created_at, id"
            ).fetchall()
            for row in rows:
                version = row["version"] + 1
                connection.execute(
                    "UPDATE runs SET state = 'interrupted', version = ?, stop_reason = 'process_restart', "
                    "updated_at = ? WHERE id = ?", (version, _now(), row["id"]),
                )
                self._insert_event(connection, row["id"], "state_changed", {
                    "from": row["state"], "to": "interrupted", "version": version, "reason": "process_restart",
                })
            return [row["id"] for row in rows]
