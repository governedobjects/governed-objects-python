# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Brafford Labs, LLC
"""Accepted-history classification and single-lineage invariants.

A stored row is an accepted revision record exactly when it carries a recorded
history position (``SQLiteStore.list_accepted_for_object``). The exporter and
the replayer both read accepted history through that query. Unpositioned rows
are operational audit: denials, conflicts, refusals, provider failures and
reads. An unpositioned ALLOW row other than a read is a record written before
positions existed; an object holding one has legacy history.
"""

from __future__ import annotations

from itertools import groupby

from governed_objects.domain.models import Decision, GovernanceEvent
from governed_objects.persistence.sqlite import SQLiteStore


def is_accepted_revision(row: GovernanceEvent) -> bool:
    return row.history_position is not None


def is_read_audit(row: GovernanceEvent) -> bool:
    return row.action == "read" or row.event_type == "object.read"


def is_legacy_accepted_row(row: GovernanceEvent) -> bool:
    return (
        not is_accepted_revision(row)
        and row.decision == Decision.ALLOW
        and not is_read_audit(row)
    )


def has_legacy_history(store: SQLiteStore, object_id: str) -> bool:
    return any(is_legacy_accepted_row(r) for r in store.list_for_object(object_id))


def lineage_violations(store: SQLiteStore, object_id: str) -> list[str]:
    """Violations of the single accepted lineage for one object.

    Each positioned record is one accepted revision, so every record's
    resulting Object Version must be distinct (adjacent records sharing a
    token are a reused token, not one revision). Positions must be 1..n, and
    the current Object Version must be the last revision's token.
    """
    violations: list[str] = []
    obj = store.get(object_id)
    rows = store.list_accepted_for_object(object_id)
    positions = [r.history_position for r in rows]
    if positions != list(range(1, len(rows) + 1)):
        violations.append(f"positions are not 1..n: {positions}")
    tokens = [r.object_version for r in rows]
    for token, group in groupby(sorted(tokens)):
        count = len(list(group))
        if count > 1:
            violations.append(f"Object Version {token} names {count} accepted revisions")
    if rows and rows[0].action != "create":
        violations.append(f"first accepted record is {rows[0].action!r}, not create")
    if obj is not None:
        if not tokens:
            violations.append(f"current Object Version {obj.version} has no accepted revision")
        elif obj.version != tokens[-1]:
            violations.append(
                f"current Object Version {obj.version} is not the last revision's {tokens[-1]}"
            )
    if has_legacy_history(store, object_id):
        violations.append("unpositioned accepted rows present (legacy history)")
    return violations
