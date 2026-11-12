# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Brafford Labs, LLC
"""SQLite persistence for governed objects."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from functools import wraps
from pathlib import Path
from threading import RLock
from typing import Any, Protocol

from governed_objects.domain.models import Actor, ActorType, Decision, GovernedObject, GovernanceEvent, Relationship

INT64_MAX = 2**63 - 1


def _connection_locked(method):
    """Serialize a complete synchronous store/sidecar operation, including reads."""
    @wraps(method)
    def locked(self, *args, **kwargs):
        with self._connection_guard:
            return method(self, *args, **kwargs)
    return locked


class ObjectVersionSpaceExhausted(Exception):
    """No unused Object Version remains for the object (OBJECT_VERSION_SPACE_EXHAUSTED)."""

    code = "OBJECT_VERSION_SPACE_EXHAUSTED"

    def __init__(self, object_id: str) -> None:
        super().__init__(f"object version space exhausted: {object_id}")
        self.object_id = object_id


class IdentityAlreadyExists(Exception):
    def __init__(self, identities: list[str]) -> None:
        super().__init__(f"identities already present: {identities}")
        self.identities = identities


class ObjectRepository(Protocol):
    def save(self, obj: GovernedObject) -> None: ...
    def get(self, object_id: str) -> GovernedObject | None: ...
    def list_all(self) -> list[GovernedObject]: ...
    def list_by_type(self, object_type: str) -> list[GovernedObject]: ...
    def list_by_state(self, object_type: str, state: str) -> list[GovernedObject]: ...


class EventStore(Protocol):
    def append(self, event: GovernanceEvent) -> None: ...
    def list_for_object(self, object_id: str) -> list[GovernanceEvent]: ...
    def list_all(self) -> list[GovernanceEvent]: ...


class RelationshipStore(Protocol):
    def add(self, rel: Relationship) -> None: ...
    def list_for_source(self, source_id: str) -> list[Relationship]: ...
    def list_for_target(self, target_id: str) -> list[Relationship]: ...
    def list_all(self) -> list[Relationship]: ...


class ActorRepository(Protocol):
    def save(self, actor: Actor) -> None: ...
    def get(self, actor_id: str) -> Actor | None: ...


class SQLiteStore:
    """Combined SQLite persistence for objects, events, and relationships."""

    def __init__(self, db_path: Path) -> None:
        self._connection_guard = RLock()
        self._db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._observation_transaction_active = False
        self._init_schema()

    @_connection_locked
    def close(self) -> None:
        if self._observation_transaction_active:
            raise RuntimeError("operation requires an autonomous connection")
        self._conn.close()

    @contextmanager
    def observation_transaction(self):
        """Owned local admission only; callbacks/provider I/O stay outside."""
        with self._connection_guard:
            self._require_autonomous()
            self._conn.execute("BEGIN IMMEDIATE")
            self._observation_transaction_active = True
            try:
                yield
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise
            finally:
                self._observation_transaction_active = False

    def _require_autonomous(self) -> None:
        # Called under the shared guard, before any SQL/DDL or close effects.
        if self._observation_transaction_active or self._conn.in_transaction:
            raise RuntimeError("operation requires an autonomous connection")

    @_connection_locked
    def _commit_if_autonomous(self) -> None:
        if not self._observation_transaction_active:
            self._conn.commit()

    @_connection_locked
    def _init_schema(self) -> None:
        self._require_autonomous()
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS objects (
                id TEXT PRIMARY KEY,
                type TEXT NOT NULL,
                type_version TEXT NOT NULL DEFAULT '1',
                version INTEGER NOT NULL,
                state TEXT NOT NULL,
                data_json TEXT NOT NULL,
                policy_json TEXT NOT NULL,
                contract_json TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                storage_ref TEXT
            );
            CREATE TABLE IF NOT EXISTS events (
                event_id TEXT PRIMARY KEY,
                timestamp TEXT NOT NULL,
                object_id TEXT NOT NULL,
                object_type TEXT NOT NULL,
                type_version TEXT,
                actor_id TEXT NOT NULL,
                actor_type TEXT NOT NULL,
                action TEXT NOT NULL,
                decision TEXT NOT NULL,
                event_type TEXT NOT NULL,
                previous_state TEXT,
                new_state TEXT,
                reason TEXT,
                object_version INTEGER,
                details_json TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_events_object ON events(object_id);
            CREATE TABLE IF NOT EXISTS relationships (
                source_id TEXT NOT NULL,
                relation TEXT NOT NULL,
                target_id TEXT NOT NULL,
                target_type TEXT,
                metadata_json TEXT DEFAULT '{}',
                PRIMARY KEY (source_id, relation, target_id)
            );
            CREATE INDEX IF NOT EXISTS idx_rel_source ON relationships(source_id);
            CREATE INDEX IF NOT EXISTS idx_rel_target ON relationships(target_id);
            """
        )
        self._conn.commit()
        self._migrate_schema()

    @_connection_locked
    def _migrate_schema(self) -> None:
        self._require_autonomous()
        rel_cols = {row[1] for row in self._conn.execute("PRAGMA table_info(relationships)")}
        if "metadata_json" not in rel_cols:
            self._conn.execute(
                "ALTER TABLE relationships ADD COLUMN metadata_json TEXT DEFAULT '{}'"
            )
            self._conn.commit()
        obj_cols = {row[1] for row in self._conn.execute("PRAGMA table_info(objects)")}
        if "type_version" not in obj_cols:
            self._conn.execute(
                "ALTER TABLE objects ADD COLUMN type_version TEXT NOT NULL DEFAULT '1'"
            )
            self._conn.commit()
        event_cols = {row[1] for row in self._conn.execute("PRAGMA table_info(events)")}
        if "type_version" not in event_cols:
            self._conn.execute("ALTER TABLE events ADD COLUMN type_version TEXT")
            self._conn.commit()
        # EXPERIMENTAL. Legacy rows keep NULL:
        # positions are never back-filled from timestamps or rowid.
        if "history_position" not in event_cols:
            self._conn.execute("ALTER TABLE events ADD COLUMN history_position INTEGER")
            self._conn.commit()
        self._conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_events_history_position "
            "ON events(object_id, history_position) WHERE history_position IS NOT NULL"
        )
        if "governance_reference" not in obj_cols:
            self._conn.execute("ALTER TABLE objects ADD COLUMN governance_reference TEXT")
        # EXPERIMENTAL. NULL means the
        # timestamp column is the acceptance instant (every native row).
        if "accepted_at_recorded" not in event_cols:
            self._conn.execute("ALTER TABLE events ADD COLUMN accepted_at_recorded INTEGER")
        # EXPERIMENTAL. Highest Object
        # Version ever issued for the object: allocation metadata, never
        # canonical state and never exported. NULL means "equal to version",
        # which holds for every object whose versions only ever increased.
        if "version_high_water" not in obj_cols:
            self._conn.execute("ALTER TABLE objects ADD COLUMN version_high_water INTEGER")
        # Import provenance is Operational Metadata, kept outside accepted history.
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS interchange_imports (
                import_id TEXT PRIMARY KEY,
                record_json TEXT NOT NULL
            )
            """
        )
        self._conn.commit()

    # --- Objects ---

    @_connection_locked
    def save(self, obj: GovernedObject) -> None:
        self._conn.execute(
            """
            INSERT INTO objects (id, type, type_version, version, state, data_json, policy_json,
                                 contract_json, metadata_json, storage_ref, governance_reference,
                                 version_high_water)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                type=excluded.type,
                type_version=excluded.type_version,
                version_high_water=MAX(COALESCE(objects.version_high_water, objects.version),
                                       excluded.version),
                version=excluded.version,
                state=excluded.state,
                data_json=excluded.data_json,
                policy_json=excluded.policy_json,
                contract_json=excluded.contract_json,
                metadata_json=excluded.metadata_json,
                storage_ref=excluded.storage_ref,
                governance_reference=excluded.governance_reference
            """,
            (
                obj.id,
                obj.type,
                obj.type_version,
                obj.version,
                obj.state,
                json.dumps(obj.data),
                json.dumps(obj.policy),
                json.dumps(obj.contract),
                json.dumps(obj.metadata),
                obj.storage_ref,
                obj.governance_reference,
                obj.version,
            ),
        )
        self._commit_if_autonomous()

    @_connection_locked
    def version_high_water(self, object_id: str) -> int | None:
        """Highest Object Version ever issued for the object (EXPERIMENTAL
        allocation metadata), or None when the object does not exist."""
        row = self._conn.execute(
            "SELECT COALESCE(version_high_water, version) FROM objects WHERE id = ?", (object_id,)
        ).fetchone()
        return None if row is None else row[0]

    @_connection_locked
    def next_object_version(self, obj: GovernedObject) -> int | None:
        """Next unused Object Version for obj, or None when the version space
        is exhausted.

        Allocation is implementation-defined: one above the highest version ever
        issued for the identity (imported tokens included), so a new version never
        equals any prior one even when imported history is not monotonic. Only
        saving the accepted change advances the high-water mark.
        """
        issued = self.version_high_water(obj.id)
        top = obj.version if issued is None else max(obj.version, issued)
        if top >= INT64_MAX:
            return None
        return top + 1

    @_connection_locked
    def get(self, object_id: str) -> GovernedObject | None:
        row = self._conn.execute("SELECT * FROM objects WHERE id = ?", (object_id,)).fetchone()
        if row is None:
            return None
        return self._row_to_object(row)

    @_connection_locked
    def list_all(self) -> list[GovernedObject]:
        rows = self._conn.execute("SELECT * FROM objects ORDER BY id").fetchall()
        return [self._row_to_object(r) for r in rows]

    @_connection_locked
    def list_by_type(self, object_type: str) -> list[GovernedObject]:
        rows = self._conn.execute(
            "SELECT * FROM objects WHERE type = ? ORDER BY id", (object_type,)
        ).fetchall()
        return [self._row_to_object(r) for r in rows]

    @_connection_locked
    def list_by_types(self, object_types: list[str]) -> list[GovernedObject]:
        if not object_types:
            return []
        placeholders = ",".join("?" for _ in object_types)
        rows = self._conn.execute(
            f"SELECT * FROM objects WHERE type IN ({placeholders}) ORDER BY id",
            object_types,
        ).fetchall()
        return [self._row_to_object(r) for r in rows]

    @_connection_locked
    def list_by_state(self, object_type: str, state: str) -> list[GovernedObject]:
        rows = self._conn.execute(
            "SELECT * FROM objects WHERE type = ? AND state = ? ORDER BY id",
            (object_type, state),
        ).fetchall()
        return [self._row_to_object(r) for r in rows]

    @staticmethod
    def _row_to_object(row: sqlite3.Row) -> GovernedObject:
        type_version = row["type_version"] if "type_version" in row.keys() else "1"
        return GovernedObject(
            id=row["id"],
            type=row["type"],
            type_version=type_version,
            version=row["version"],
            state=row["state"],
            data=json.loads(row["data_json"]),
            policy=json.loads(row["policy_json"]),
            contract=json.loads(row["contract_json"]),
            metadata=json.loads(row["metadata_json"]),
            storage_ref=row["storage_ref"],
            governance_reference=(
                row["governance_reference"] if "governance_reference" in row.keys() else None
            ),
        )

    # --- Events ---

    @_connection_locked
    def append(self, event: GovernanceEvent) -> None:
        self._conn.execute(
            """
            INSERT INTO events (event_id, timestamp, object_id, object_type, type_version,
                                actor_id, actor_type, action, decision, event_type,
                                previous_state, new_state, reason, object_version, details_json,
                                history_position)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (*self._event_values(event), event.history_position),
        )
        self._commit_if_autonomous()

    @_connection_locked
    def append_accepted(self, event: GovernanceEvent) -> GovernanceEvent:
        """Append an accepted canonical change and assign its per-object
        accepted-history position (1 + highest position already recorded for
        the object) in the same statement. Returns the event with its position.

        EXPERIMENTAL. The position is the only
        source of accepted-history order; timestamps and rowid are not used.
        """
        self._conn.execute(
            """
            INSERT INTO events (event_id, timestamp, object_id, object_type, type_version,
                                actor_id, actor_type, action, decision, event_type,
                                previous_state, new_state, reason, object_version, details_json,
                                history_position)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    (SELECT COALESCE(MAX(history_position), 0) + 1
                       FROM events WHERE object_id = ?))
            """,
            (*self._event_values(event), event.object_id),
        )
        self._commit_if_autonomous()
        row = self._conn.execute(
            "SELECT history_position FROM events WHERE event_id = ?", (event.event_id,)
        ).fetchone()
        return event.model_copy(update={"history_position": row["history_position"]})

    @staticmethod
    def _event_values(event: GovernanceEvent) -> tuple:
        return (
            event.event_id,
            event.timestamp.isoformat(),
            event.object_id,
            event.object_type,
            event.type_version,
            event.actor_id,
            event.actor_type,
            event.action,
            event.decision.value,
            event.event_type,
            event.previous_state,
            event.new_state,
            event.reason,
            event.object_version,
            json.dumps(event.details),
        )

    @_connection_locked
    def list_for_object(self, object_id: str) -> list[GovernanceEvent]:
        rows = self._conn.execute(
            "SELECT * FROM events WHERE object_id = ? ORDER BY timestamp",
            (object_id,),
        ).fetchall()
        return [self._row_to_event(r) for r in rows]

    @_connection_locked
    def list_accepted_for_object(self, object_id: str) -> list[GovernanceEvent]:
        """Rows carrying an accepted-history position, in position order."""
        rows = self._conn.execute(
            "SELECT * FROM events WHERE object_id = ? AND history_position IS NOT NULL "
            "ORDER BY history_position",
            (object_id,),
        ).fetchall()
        return [self._row_to_event(r) for r in rows]

    @_connection_locked
    def list_all_events(self) -> list[GovernanceEvent]:
        rows = self._conn.execute("SELECT * FROM events ORDER BY timestamp").fetchall()
        return [self._row_to_event(r) for r in rows]

    @staticmethod
    def _row_to_event(row: sqlite3.Row) -> GovernanceEvent:
        ts = datetime.fromisoformat(row["timestamp"])
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        return GovernanceEvent(
            event_id=row["event_id"],
            timestamp=ts,
            object_id=row["object_id"],
            object_type=row["object_type"],
            type_version=row["type_version"] if "type_version" in row.keys() else None,
            actor_id=row["actor_id"],
            actor_type=row["actor_type"],
            action=row["action"],
            decision=Decision(row["decision"]),
            event_type=row["event_type"],
            previous_state=row["previous_state"],
            new_state=row["new_state"],
            reason=row["reason"] or "",
            object_version=row["object_version"] or 1,
            details=json.loads(row["details_json"] or "{}"),
            history_position=(
                row["history_position"] if "history_position" in row.keys() else None
            ),
            accepted_at_recorded=(
                "accepted_at_recorded" not in row.keys()
                or row["accepted_at_recorded"] is None
                or bool(row["accepted_at_recorded"])
            ),
        )

    # --- Interchange import (EXPERIMENTAL) ---

    @_connection_locked
    def identities_present(self, identities: list[str]) -> list[str]:
        """Identities for which this store holds an object, an accepted
        history row, or a canonical outgoing relationship."""
        return [i for i in identities if self._identity_present(i)]

    def _identity_present(self, identity: str) -> bool:
        conn = self._conn
        return bool(
            conn.execute("SELECT 1 FROM objects WHERE id = ?", (identity,)).fetchone()
            or conn.execute(
                "SELECT 1 FROM events WHERE object_id = ? AND history_position IS NOT NULL",
                (identity,),
            ).fetchone()
            or conn.execute(
                "SELECT 1 FROM relationships WHERE source_id = ?", (identity,)
            ).fetchone()
        )

    @_connection_locked
    def restore_imported(
        self,
        objects: list[GovernedObject],
        events: list[GovernanceEvent],
        relationships: list[Relationship],
        record: dict[str, Any],
    ) -> None:
        """Write a fully validated import in ONE transaction: the objects, their
        accepted history rows (positions and versions exactly as given), their
        canonical relationships, and the import provenance record. The identity
        check is repeated inside the transaction; on any failure everything is
        rolled back and :class:`IdentityAlreadyExists` or the error propagates."""
        conn = self._conn
        with self.observation_transaction():
            present = [o.id for o in objects if self._identity_present(o.id)]
            if present:
                raise IdentityAlreadyExists(present)
            for obj in objects:
                self._insert_restored_object(obj)
            for event in events:
                self._insert_restored_event(event)
            for rel in relationships:
                self._insert_restored_relationship(rel)
            for obj in objects:
                self._initialize_version_high_water(obj.id)
            conn.execute(
                "INSERT INTO interchange_imports (import_id, record_json) VALUES (?, ?)",
                (record["import_id"], json.dumps(record, sort_keys=True)),
            )

    def _insert_restored_object(self, obj: GovernedObject) -> None:
        self._conn.execute(
            """
            INSERT INTO objects (id, type, type_version, version, state, data_json, policy_json,
                                 contract_json, metadata_json, storage_ref, governance_reference)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                obj.id, obj.type, obj.type_version, obj.version, obj.state,
                json.dumps(obj.data), json.dumps(obj.policy), json.dumps(obj.contract),
                json.dumps(obj.metadata), obj.storage_ref, obj.governance_reference,
            ),
        )

    def _initialize_version_high_water(self, object_id: str) -> None:
        # Imported tokens need not be monotonic, so the current version alone
        # does not bound them.
        self._conn.execute(
            """
            UPDATE objects SET version_high_water = MAX(version, COALESCE((
                SELECT MAX(object_version) FROM events
                WHERE events.object_id = objects.id AND history_position IS NOT NULL
            ), version))
            WHERE id = ?
            """,
            (object_id,),
        )

    def _insert_restored_event(self, event: GovernanceEvent) -> None:
        self._conn.execute(
            """
            INSERT INTO events (event_id, timestamp, object_id, object_type, type_version,
                                actor_id, actor_type, action, decision, event_type,
                                previous_state, new_state, reason, object_version, details_json,
                                history_position, accepted_at_recorded)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (*self._event_values(event), event.history_position, int(event.accepted_at_recorded)),
        )

    def _insert_restored_relationship(self, rel: Relationship) -> None:
        self._conn.execute(
            """
            INSERT INTO relationships (source_id, relation, target_id, target_type, metadata_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (rel.source_id, rel.relation, rel.target_id, rel.target_type, json.dumps(rel.metadata)),
        )

    @_connection_locked
    def list_imports(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT record_json FROM interchange_imports ORDER BY rowid"
        ).fetchall()
        return [json.loads(r["record_json"]) for r in rows]

    @_connection_locked
    def dump_tables(self) -> dict[str, list[dict[str, Any]]]:
        """Every row of every table, in a deterministic order (diagnostic)."""
        out: dict[str, list[dict[str, Any]]] = {}
        tables = [
            r[0]
            for r in self._conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            )
        ]
        for table in tables:
            rows = [dict(r) for r in self._conn.execute(f'SELECT * FROM "{table}"')]
            out[table] = sorted(rows, key=lambda r: json.dumps(r, sort_keys=True, default=str))
        return out

    # --- Relationships ---

    @_connection_locked
    def add(self, rel: Relationship) -> None:
        self._conn.execute(
            """
            INSERT OR REPLACE INTO relationships (source_id, relation, target_id, target_type, metadata_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                rel.source_id,
                rel.relation,
                rel.target_id,
                rel.target_type,
                json.dumps(rel.metadata),
            ),
        )
        self._commit_if_autonomous()

    @_connection_locked
    def remove(self, source_id: str, relation: str, target_id: str) -> None:
        self._conn.execute(
            "DELETE FROM relationships WHERE source_id = ? AND relation = ? AND target_id = ?",
            (source_id, relation, target_id),
        )
        self._commit_if_autonomous()

    @_connection_locked
    def list_for_source(self, source_id: str) -> list[Relationship]:
        rows = self._conn.execute(
            "SELECT * FROM relationships WHERE source_id = ?", (source_id,)
        ).fetchall()
        return [self._row_to_rel(r) for r in rows]

    @_connection_locked
    def list_for_target(self, target_id: str) -> list[Relationship]:
        rows = self._conn.execute(
            "SELECT * FROM relationships WHERE target_id = ?", (target_id,)
        ).fetchall()
        return [self._row_to_rel(r) for r in rows]

    @_connection_locked
    def list_all_relationships(self) -> list[Relationship]:
        rows = self._conn.execute("SELECT * FROM relationships").fetchall()
        return [self._row_to_rel(r) for r in rows]

    @staticmethod
    def _row_to_rel(row: sqlite3.Row) -> Relationship:
        meta_raw = row["metadata_json"] if "metadata_json" in row.keys() else "{}"
        return Relationship(
            source_id=row["source_id"],
            relation=row["relation"],
            target_id=row["target_id"],
            target_type=row["target_type"],
            metadata=json.loads(meta_raw or "{}"),
        )
