# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Brafford Labs, LLC
"""SQLite persistence for external observations and review cases."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from governed_objects.observations.models import (
    ExternalObservation,
    FieldChange,
    ObservationDecision,
    ObservationResult,
    ObservationSource,
)
from governed_objects.persistence.sqlite import SQLiteStore, _connection_locked


class ObservationStore:
    def __init__(self, store: SQLiteStore) -> None:
        self._store = store
        self._connection_guard = store._connection_guard
        self._ensure_schema()

    @_connection_locked
    def _ensure_schema(self) -> None:
        self._store._require_autonomous()
        self._store._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS observations (
                id TEXT PRIMARY KEY,
                object_id TEXT NOT NULL,
                provider_name TEXT NOT NULL,
                external_id TEXT,
                provider_type TEXT,
                observed_at TEXT NOT NULL,
                source TEXT NOT NULL,
                dedupe_key TEXT UNIQUE,
                changes_json TEXT NOT NULL,
                state_change_json TEXT,
                provider_actor_id TEXT,
                provider_event_id TEXT,
                provider_version TEXT,
                provider_sequence INTEGER,
                raw_metadata_json TEXT,
                decision TEXT,
                decision_reason TEXT,
                canonical_event_id TEXT,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_observations_object ON observations(object_id);
            CREATE TABLE IF NOT EXISTS review_cases (
                id TEXT PRIMARY KEY,
                object_id TEXT NOT NULL,
                observation_id TEXT NOT NULL,
                field_name TEXT NOT NULL,
                canonical_value TEXT,
                observed_value TEXT,
                state TEXT NOT NULL,
                resolved_by TEXT,
                resolution TEXT,
                canonical_event_id TEXT,
                created_at TEXT NOT NULL,
                resolved_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_review_cases_object ON review_cases(object_id);
            """
        )
        self._store._conn.commit()

    @property
    @_connection_locked
    def stats(self) -> dict[str, int]:
        received = self._store._conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
        deduped = self._store._conn.execute(
            "SELECT COUNT(*) FROM observations WHERE decision = ?", (ObservationDecision.NO_CHANGE.value,)
        ).fetchone()[0]
        emitted = self._store._conn.execute(
            "SELECT COUNT(*) FROM observations WHERE canonical_event_id IS NOT NULL"
        ).fetchone()[0]
        return {
            "observations_received": received,
            "observations_deduplicated": deduped,
            "canonical_changes_emitted": emitted,
        }

    @_connection_locked
    def exists_dedupe_key(self, key: str) -> bool:
        row = self._store._conn.execute(
            "SELECT 1 FROM observations WHERE dedupe_key = ?", (key,)
        ).fetchone()
        return row is not None

    @_connection_locked
    def save_observation(
        self,
        obs: ExternalObservation,
        *,
        decision: ObservationDecision,
        reason: str = "",
        canonical_event_id: str | None = None,
    ) -> None:
        now = datetime.now(UTC).isoformat()
        self._store._conn.execute(
            """
            INSERT INTO observations (
                id, object_id, provider_name, external_id, provider_type,
                observed_at, source, dedupe_key, changes_json, state_change_json,
                provider_actor_id, provider_event_id, provider_version, provider_sequence,
                raw_metadata_json, decision, decision_reason, canonical_event_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                obs.id,
                obs.object_id,
                obs.provider_name,
                obs.external_id,
                obs.provider_type,
                obs.observed_at.isoformat(),
                obs.source.value,
                obs.dedupe_key(),
                json.dumps({k: v.model_dump() for k, v in obs.changes.items()}),
                json.dumps(obs.state_change.model_dump()) if obs.state_change else None,
                obs.provider_actor_id,
                obs.provider_event_id,
                obs.provider_version,
                obs.provider_sequence,
                json.dumps(obs.raw_metadata),
                decision.value,
                reason,
                canonical_event_id,
                now,
            ),
        )
        self._store._commit_if_autonomous()

    @_connection_locked
    def list_for_object(self, object_id: str) -> list[dict[str, Any]]:
        rows = self._store._conn.execute(
            "SELECT * FROM observations WHERE object_id = ? ORDER BY created_at",
            (object_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    @_connection_locked
    def create_review_case(
        self,
        *,
        case_id: str,
        object_id: str,
        observation_id: str,
        field_name: str,
        canonical_value: Any,
        observed_value: Any,
    ) -> None:
        now = datetime.now(UTC).isoformat()
        self._store._conn.execute(
            """
            INSERT INTO review_cases (
                id, object_id, observation_id, field_name,
                canonical_value, observed_value, state, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'open', ?)
            """,
            (
                case_id,
                object_id,
                observation_id,
                field_name,
                json.dumps(canonical_value),
                json.dumps(observed_value),
                now,
            ),
        )
        self._store._commit_if_autonomous()

    @_connection_locked
    def get_review_case(self, case_id: str) -> dict[str, Any] | None:
        row = self._store._conn.execute(
            "SELECT * FROM review_cases WHERE id = ?", (case_id,)
        ).fetchone()
        return dict(row) if row else None

    @_connection_locked
    def resolve_review_case(
        self,
        case_id: str,
        *,
        resolution: str,
        resolved_by: str,
        canonical_event_id: str | None = None,
    ) -> None:
        now = datetime.now(UTC).isoformat()
        self._store._conn.execute(
            """
            UPDATE review_cases SET
                state = ?,
                resolution = ?,
                resolved_by = ?,
                canonical_event_id = ?,
                resolved_at = ?
            WHERE id = ?
            """,
            (
                "accepted" if resolution == "ACCEPT_EXTERNAL" else "rejected",
                resolution,
                resolved_by,
                canonical_event_id,
                now,
                case_id,
            ),
        )
        self._store._commit_if_autonomous()
