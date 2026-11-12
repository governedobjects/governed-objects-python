# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Brafford Labs, LLC
"""SQLite-backed durable idempotency and command attempt tracking."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from governed_objects.domain.models import CommandResult, Decision
from governed_objects.persistence.sqlite import SQLiteStore, _connection_locked


class IdempotencyStore:
    """Persists idempotent command results and attempt metadata."""

    def __init__(self, store: SQLiteStore) -> None:
        self._store = store
        self._connection_guard = store._connection_guard
        self._ensure_schema()

    @_connection_locked
    def _ensure_schema(self) -> None:
        self._store._require_autonomous()
        self._store._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS command_attempts (
                command_id TEXT NOT NULL,
                object_id TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                correlation_id TEXT,
                status TEXT NOT NULL,
                provider_outcome TEXT,
                result_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (object_id, idempotency_key)
            );
            CREATE INDEX IF NOT EXISTS idx_command_attempts_command
                ON command_attempts(command_id);
            """
        )
        self._store._conn.commit()

    @_connection_locked
    def get(self, object_id: str, idempotency_key: str) -> CommandResult | None:
        row = self._store._conn.execute(
            """
            SELECT result_json, status FROM command_attempts
            WHERE object_id = ? AND idempotency_key = ?
            """,
            (object_id, idempotency_key),
        ).fetchone()
        if row is None or not row["result_json"]:
            return None
        if row["status"] != "succeeded":
            return None
        return _result_from_json(row["result_json"])

    @_connection_locked
    def put(
        self,
        object_id: str,
        idempotency_key: str,
        result: CommandResult,
        *,
        command_id: str | None = None,
        correlation_id: str | None = None,
    ) -> None:
        now = datetime.now(UTC).isoformat()
        self._store._conn.execute(
            """
            INSERT INTO command_attempts (
                command_id, object_id, idempotency_key, correlation_id,
                status, provider_outcome, result_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'succeeded', NULL, ?, ?, ?)
            ON CONFLICT(object_id, idempotency_key) DO UPDATE SET
                command_id=excluded.command_id,
                correlation_id=excluded.correlation_id,
                status=excluded.status,
                result_json=excluded.result_json,
                updated_at=excluded.updated_at
            """,
            (
                command_id or "",
                object_id,
                idempotency_key,
                correlation_id,
                json.dumps(_result_to_json(result)),
                now,
                now,
            ),
        )
        self._store._commit_if_autonomous()

    @_connection_locked
    def record_attempt(
        self,
        *,
        command_id: str,
        object_id: str,
        idempotency_key: str,
        correlation_id: str | None,
        status: str,
        provider_outcome: str | None = None,
    ) -> None:
        now = datetime.now(UTC).isoformat()
        self._store._conn.execute(
            """
            INSERT INTO command_attempts (
                command_id, object_id, idempotency_key, correlation_id,
                status, provider_outcome, result_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?)
            ON CONFLICT(object_id, idempotency_key) DO UPDATE SET
                command_id=excluded.command_id,
                correlation_id=excluded.correlation_id,
                status=excluded.status,
                provider_outcome=excluded.provider_outcome,
                updated_at=excluded.updated_at
            """,
            (
                command_id,
                object_id,
                idempotency_key,
                correlation_id,
                status,
                provider_outcome,
                now,
                now,
            ),
        )
        self._store._commit_if_autonomous()


def _result_to_json(result: CommandResult) -> dict[str, Any]:
    return {
        "decision": result.decision.value,
        "reason": result.reason,
        "action": result.action,
        "object_id": result.object_id,
        "previous_state": result.previous_state,
        "new_state": result.new_state,
        "object_version": result.object_version,
        "data": result.data,
    }


def _result_from_json(raw: str) -> CommandResult:
    data = json.loads(raw)
    return CommandResult(
        decision=Decision(data["decision"]),
        reason=data.get("reason", ""),
        action=data.get("action", ""),
        object_id=data.get("object_id"),
        previous_state=data.get("previous_state"),
        new_state=data.get("new_state"),
        object_version=data.get("object_version"),
        data=data.get("data"),
    )
