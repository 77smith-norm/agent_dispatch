from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from agent_dispatch.models import DispatchRecord, DispatchRequest, DispatchState


class WalkieTalkieViolation(RuntimeError):
    pass


class InvalidStateTransition(RuntimeError):
    pass


class DispatchDB:
    def __init__(self, path: str | Path, *, timeout: float = 5.0) -> None:
        self.path = Path(path)
        self.timeout = timeout
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def can_dispatch(self, agent_id: str) -> bool:
        with self._connect() as connection:
            return not self._has_pending(connection, agent_id)

    def validate_walkie_talkie(self, agent_id: str) -> None:
        if not self.can_dispatch(agent_id):
            raise WalkieTalkieViolation(f"agent {agent_id!r} already has a pending dispatch")

    def record_pending(self, request: DispatchRequest) -> DispatchRecord:
        created_at = _utcnow().isoformat()
        request_id = uuid4().hex
        request_json = json.dumps(request.model_dump(mode="json"), sort_keys=True)

        def operation(connection: sqlite3.Connection) -> int:
            if self._has_pending(connection, request.agent_id):
                raise WalkieTalkieViolation(
                    f"agent {request.agent_id!r} already has a pending dispatch"
                )

            try:
                cursor = connection.execute(
                    """
                    INSERT INTO dispatches (
                        request_id,
                        agent_id,
                        endpoint,
                        thread_id,
                        request_json,
                        state,
                        created_at,
                        updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        request_id,
                        request.agent_id,
                        str(request.endpoint),
                        request.thread.id,
                        request_json,
                        DispatchState.PENDING.value,
                        created_at,
                        created_at,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                if "one_pending_per_agent" in str(exc):
                    raise WalkieTalkieViolation(
                        f"agent {request.agent_id!r} already has a pending dispatch"
                    ) from exc

                raise

            dispatch_id = cursor.lastrowid
            if dispatch_id is None:
                raise RuntimeError("sqlite insert did not return a dispatch id")

            return dispatch_id

        dispatch_id = self._write(operation)
        return self.get_dispatch(dispatch_id)

    def mark_replied(self, dispatch_id: int, response: Any) -> DispatchRecord:
        response_json = json.dumps(response, sort_keys=True)
        completed_at = _utcnow().isoformat()

        def operation(connection: sqlite3.Connection) -> None:
            cursor = connection.execute(
                """
                UPDATE dispatches
                SET state = ?,
                    response_json = ?,
                    error_message = NULL,
                    completed_at = ?,
                    updated_at = ?
                WHERE id = ? AND state = ?
                """,
                (
                    DispatchState.REPLIED.value,
                    response_json,
                    completed_at,
                    completed_at,
                    dispatch_id,
                    DispatchState.PENDING.value,
                ),
            )
            self._ensure_transition(cursor.rowcount, connection, dispatch_id)

        self._write(operation)
        return self.get_dispatch(dispatch_id)

    def mark_failed(self, dispatch_id: int, error_message: str) -> DispatchRecord:
        completed_at = _utcnow().isoformat()

        def operation(connection: sqlite3.Connection) -> None:
            cursor = connection.execute(
                """
                UPDATE dispatches
                SET state = ?,
                    response_json = NULL,
                    error_message = ?,
                    completed_at = ?,
                    updated_at = ?
                WHERE id = ? AND state = ?
                """,
                (
                    DispatchState.FAILED.value,
                    error_message,
                    completed_at,
                    completed_at,
                    dispatch_id,
                    DispatchState.PENDING.value,
                ),
            )
            self._ensure_transition(cursor.rowcount, connection, dispatch_id)

        self._write(operation)
        return self.get_dispatch(dispatch_id)

    def get_dispatch(self, dispatch_id: int) -> DispatchRecord:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM dispatches WHERE id = ?",
                (dispatch_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown dispatch id {dispatch_id}")

            return self._row_to_dispatch(row)

    def list_dispatches(self, *, agent_id: str | None = None) -> list[DispatchRecord]:
        query = "SELECT * FROM dispatches"
        params: tuple[str, ...] = ()

        if agent_id is not None:
            query += " WHERE agent_id = ?"
            params = (agent_id,)

        query += " ORDER BY id ASC"

        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()

        return [self._row_to_dispatch(row) for row in rows]

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS dispatches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_id TEXT NOT NULL UNIQUE,
                    agent_id TEXT NOT NULL,
                    endpoint TEXT NOT NULL,
                    thread_id TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    state TEXT NOT NULL
                        CHECK (state IN ('PENDING', 'REPLIED', 'FAILED')),
                    response_json TEXT,
                    error_message TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT,
                    CHECK (
                        (state = 'PENDING'
                            AND response_json IS NULL
                            AND error_message IS NULL
                            AND completed_at IS NULL)
                        OR
                        (state = 'REPLIED'
                            AND response_json IS NOT NULL
                            AND error_message IS NULL
                            AND completed_at IS NOT NULL)
                        OR
                        (state = 'FAILED'
                            AND response_json IS NULL
                            AND error_message IS NOT NULL
                            AND completed_at IS NOT NULL)
                    )
                )
                """
            )
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS one_pending_per_agent
                ON dispatches(agent_id)
                WHERE state = 'PENDING'
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=self.timeout,
            isolation_level=None,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {int(self.timeout * 1000)}")
        return connection

    def _has_pending(self, connection: sqlite3.Connection, agent_id: str) -> bool:
        row = connection.execute(
            """
            SELECT 1
            FROM dispatches
            WHERE agent_id = ? AND state = ?
            LIMIT 1
            """,
            (agent_id, DispatchState.PENDING.value),
        ).fetchone()
        return row is not None

    def _write(self, operation: Callable[[sqlite3.Connection], Any]) -> Any:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                result = operation(connection)
            except Exception:
                connection.rollback()
                raise

            connection.commit()
            return result

    def _ensure_transition(
        self,
        rowcount: int,
        connection: sqlite3.Connection,
        dispatch_id: int,
    ) -> None:
        if rowcount == 1:
            return

        row = connection.execute(
            "SELECT state FROM dispatches WHERE id = ?",
            (dispatch_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown dispatch id {dispatch_id}")

        raise InvalidStateTransition(
            f"dispatch {dispatch_id} cannot transition from {row['state']}"
        )

    def _row_to_dispatch(self, row: sqlite3.Row) -> DispatchRecord:
        request = DispatchRequest.model_validate(json.loads(row["request_json"]))
        response = None
        if row["response_json"] is not None:
            response = json.loads(row["response_json"])

        return DispatchRecord(
            id=row["id"],
            request_id=row["request_id"],
            agent_id=row["agent_id"],
            endpoint=row["endpoint"],
            thread_id=row["thread_id"],
            request=request,
            state=DispatchState(row["state"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            completed_at=_parse_optional_datetime(row["completed_at"]),
            response=response,
            error_message=row["error_message"],
        )


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_optional_datetime(value: str | None) -> datetime | None:
    if value is None:
        return None

    return datetime.fromisoformat(value)
