# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Brafford Labs, LLC
"""Persistence for provider repair attempts — synchronization history only."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from governed_objects.outbound.models import ProviderRepairResult, RepairStatus
from governed_objects.persistence.sqlite import SQLiteStore, _connection_locked


class RepairStore:
    def __init__(self, store: SQLiteStore) -> None:
        self._store = store
        self._connection_guard = store._connection_guard
        self._ensure_schema()

    @_connection_locked
    def _ensure_schema(self) -> None:
        self._store._require_autonomous()
        self._store._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS provider_repairs (
                repair_id TEXT PRIMARY KEY,
                object_id TEXT NOT NULL,
                provider_name TEXT NOT NULL,
                status TEXT NOT NULL,
                reason TEXT,
                canonical_expected_version INTEGER,
                canonical_actual_version INTEGER,
                provider_expected_version TEXT,
                provider_actual_version TEXT,
                changes_json TEXT,
                correlation_id TEXT,
                originating_observation_id TEXT,
                review_case_id TEXT,
                initiated_by TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_provider_repairs_object ON provider_repairs(object_id);
            CREATE TABLE IF NOT EXISTS sync_audit (
                event_id TEXT PRIMARY KEY,
                object_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                repair_id TEXT,
                details_json TEXT,
                created_at TEXT NOT NULL
            );
            """
        )
        self._store._conn.commit()

    @_connection_locked
    def save_repair(
        self,
        result: ProviderRepairResult,
        *,
        initiated_by: str = "system",
        originating_observation_id: str | None = None,
        review_case_id: str | None = None,
    ) -> None:
        now = datetime.now(UTC).isoformat()
        self._store._conn.execute(
            """
            INSERT INTO provider_repairs (
                repair_id, object_id, provider_name, status, reason,
                canonical_expected_version, canonical_actual_version,
                provider_expected_version, provider_actual_version,
                changes_json, correlation_id, originating_observation_id,
                review_case_id, initiated_by, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(repair_id) DO UPDATE SET
                status=excluded.status,
                reason=excluded.reason,
                canonical_actual_version=excluded.canonical_actual_version,
                provider_actual_version=excluded.provider_actual_version,
                updated_at=excluded.updated_at
            """,
            (
                result.repair_id,
                result.object_id,
                result.provider_name,
                result.status.value,
                result.reason,
                result.canonical_expected_version,
                result.canonical_actual_version,
                result.provider_expected_version,
                result.provider_actual_version,
                json.dumps(result.changes),
                result.correlation_id,
                originating_observation_id,
                review_case_id,
                initiated_by,
                now,
                now,
            ),
        )
        self._store._commit_if_autonomous()

    @_connection_locked
    def append_sync_event(
        self,
        *,
        object_id: str,
        event_type: str,
        repair_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        now = datetime.now(UTC).isoformat()
        event_id = f"sync-{repair_id or object_id}-{now}"
        self._store._conn.execute(
            """
            INSERT INTO sync_audit (event_id, object_id, event_type, repair_id, details_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (event_id, object_id, event_type, repair_id, json.dumps(details or {}), now),
        )
        self._store._commit_if_autonomous()

    @_connection_locked
    def get_repair(self, repair_id: str) -> dict[str, Any] | None:
        row = self._store._conn.execute(
            "SELECT * FROM provider_repairs WHERE repair_id = ?", (repair_id,)
        ).fetchone()
        return dict(row) if row else None

    @_connection_locked
    def stats(self) -> dict[str, int]:
        applied = self._store._conn.execute(
            "SELECT COUNT(*) FROM provider_repairs WHERE status = ?", (RepairStatus.APPLIED.value,)
        ).fetchone()[0]
        conflicts = self._store._conn.execute(
            "SELECT COUNT(*) FROM provider_repairs WHERE status IN (?, ?)",
            (RepairStatus.CANONICAL_CONFLICT.value, RepairStatus.PROVIDER_CONFLICT.value),
        ).fetchone()[0]
        planned = self._store._conn.execute(
            "SELECT COUNT(*) FROM provider_repairs WHERE status = ?", (RepairStatus.PLANNED.value,)
        ).fetchone()[0]
        return {
            "repairs_planned": planned,
            "repairs_applied": applied,
            "repair_conflicts": conflicts,
        }
