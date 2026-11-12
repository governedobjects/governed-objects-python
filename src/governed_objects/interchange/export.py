# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Brafford Labs, LLC
"""Export to ``go-core-interchange-experiment/2`` with Draft 2.2 content (EXPERIMENTAL).

Accepted-history filtering follows the interchange experiment's
history-semantics analysis, sections 5.3 and 6.3:

1. a candidate row is ``decision == ALLOW`` with ``event_type ==
   "object.<action>"``;
2. its action is ``create``, ``add_relationship``, ``remove_relationship``,
   or a state/lifecycle action declared by the portable type (never
   ``read``);
3. it advanced the Object Version, which this runtime records as an
   accepted-history position assigned at append time;
4. the object has no provider binding.

Order comes only from the recorded positions. Timestamps, event IDs, and
rowid are never used. When the criteria disagree the export is refused;
nothing is repaired, reordered, defaulted, or dropped.

Export is a governed read (PRC-EXPORT-002). Read authority for every
requested object is established before any object is classified, so a
requester without it receives the same refusal whether or not an object is
exportable (PRC-REFUSE-001, AUTH-DISCLOSE-001). Existence is not protected.
"""

from __future__ import annotations

from datetime import UTC
from typing import TYPE_CHECKING, Any

from governed_objects.domain.models import Actor, Decision, GovernanceEvent, GovernedObject
from governed_objects.interchange import (
    ARTIFACT_CONTRACTS,
    EXPORT_ARTIFACT_REVISION,
    EXPORT_HISTORY_UNCLASSIFIABLE,
    EXPORT_ORDER_INDETERMINATE,
    GOVERNANCE_REFERENCE_MODEL,
    HISTORY_COMPLETENESS,
    HISTORY_POSITION_MODEL,
    UNSUPPORTED_SEMANTIC_FEATURE,
)
from governed_objects.interchange.importer import ACTION_RECORDED, DUPLICATE_IDENTITY_IN_BUNDLE
from governed_objects.events.replay import bound_type_definition
from governed_objects.interchange.types import (
    ACTION_LIFECYCLE,
    ACTION_STATE,
    lifecycle_field,
    portable_type_of,
)

if TYPE_CHECKING:
    from governed_objects.persistence.sqlite import SQLiteStore
    from governed_objects.policy.evaluator import PolicyEvaluator
    from governed_objects.runtime.runtime import GovernedObjectRuntime
    from governed_objects.types.registry import TypeRegistry

_EXPORT_STATUS, _EXPORT_SPEC_REVISION = ARTIFACT_CONTRACTS[EXPORT_ARTIFACT_REVISION]

AUTHORIZATION_DENIED = "AUTHORIZATION_DENIED"
NOT_FOUND = "NOT_FOUND"
# Independent of the object's type and policy, which could reveal exportability.
EXPORT_READ_DENIED = "requester is not authorized to read the object for export"

_RUNTIME_EFFECTS = {
    "create": "create",
    "add_relationship": "relationship_add",
    "remove_relationship": "relationship_remove",
}


class ExportRefused(Exception):
    def __init__(self, code: str, message: str, identity: str | None = None) -> None:
        super().__init__(f"{code}: {message}" + (f" ({identity})" if identity else ""))
        self.code = code
        self.message = message
        self.identity = identity


def _token(value: Any, identity: str) -> str:
    if type(value) is not int:
        raise ExportRefused(
            EXPORT_HISTORY_UNCLASSIFIABLE, f"native Object Version is not an int: {value!r}", identity
        )
    return str(value)


class InterchangeExporter:
    def __init__(
        self,
        runtime: GovernedObjectRuntime,
        store: SQLiteStore,
        types: TypeRegistry,
        policy: PolicyEvaluator,
    ) -> None:
        self._runtime = runtime
        self._store = store
        self._types = types
        self._policy = policy

    def export(
        self,
        identities: list[str],
        *,
        actor: Actor,
        closed: bool,
        include_timestamps: bool = False,
        source_implementation: str | None = None,
    ) -> dict[str, Any]:
        identities = list(identities)
        if len(set(identities)) != len(identities):
            raise ExportRefused(
                DUPLICATE_IDENTITY_IN_BUNDLE,
                "export selection contains repeated identities",
            )
        readable = [self._authorize(i, actor) for i in identities]
        objects = [self._export_object(obj, include_timestamps) for obj in readable]
        manifest: dict[str, Any] = {
            "artifact_status": _EXPORT_STATUS,
            "artifact_revision": EXPORT_ARTIFACT_REVISION,
            "spec_revision": _EXPORT_SPEC_REVISION,
            "history_completeness": HISTORY_COMPLETENESS,
            "history_position_model": HISTORY_POSITION_MODEL,
            "governance_reference_model": GOVERNANCE_REFERENCE_MODEL,
            "profiles": [],
            "identities": [o["canonical_identity"] for o in objects],
            "type_bindings": _unique(
                {"type_identity": o["type_identity"], "type_version": o["type_version"]}
                for o in objects
            ),
            "governance_references": _unique(
                o["governance_reference"] for o in objects if o["governance_reference"] is not None
            ),
            "closed": closed,
        }
        provenance: dict[str, Any] = {}
        if source_implementation:
            provenance["source_implementation"] = source_implementation
        if include_timestamps:
            provenance["exported_at"] = self._runtime.clock.now().astimezone(UTC).isoformat()
        if provenance:
            manifest["provenance"] = provenance
        return {"manifest": manifest, "objects": objects}

    def _authorize(self, identity: str, actor: Actor) -> GovernedObject:
        obj = self._runtime.get_object(identity)
        if obj is None:
            raise ExportRefused(NOT_FOUND, "no such object", identity)
        type_def = bound_type_definition(self._types, obj)
        if type_def is None:
            # Read authority cannot be established without the type's policy.
            raise ExportRefused(AUTHORIZATION_DENIED, EXPORT_READ_DENIED, identity)
        if self._policy.evaluate(actor, "read", obj, type_def).decision != Decision.ALLOW:
            raise ExportRefused(AUTHORIZATION_DENIED, EXPORT_READ_DENIED, identity)
        return obj

    def _export_object(self, obj: GovernedObject, include_timestamps: bool) -> dict[str, Any]:
        identity = obj.id
        portable = portable_type_of(self._types.get(obj.type))
        if portable is None:
            raise ExportRefused(
                UNSUPPORTED_SEMANTIC_FEATURE,
                f"type {obj.type!r} is not configured as a portable Core type",
                identity,
            )
        if obj.metadata.get("provider_binding"):
            raise ExportRefused(
                UNSUPPORTED_SEMANTIC_FEATURE, "provider-bound objects are outside Core export", identity
            )
        if obj.governance_reference is None:
            raise ExportRefused(
                UNSUPPORTED_SEMANTIC_FEATURE,
                "object has no stored portable governance reference",
                identity,
            )

        lc_field = lifecycle_field(portable)
        actions = portable.get("actions", {})
        rows = self._accepted_rows(obj, actions)
        history = [self._entry(identity, row, lc_field, actions, include_timestamps) for row in rows]

        state = dict(obj.data)
        if lc_field is not None:
            state[lc_field] = obj.state
        relationships = sorted(
            (
                {"source": r.source_id, "type": r.relation, "target": r.target_id}
                for r in self._runtime.canonical_relationships(identity)
            ),
            key=lambda r: (r["type"], r["target"]),
        )
        return {
            "canonical_identity": obj.id,
            "type_identity": obj.type,
            "type_version": obj.type_version,
            "governance_reference": obj.governance_reference,
            "accepted_state": state,
            "relationships": relationships,
            "current_object_version": _token(obj.version, identity),
            "history": history,
        }

    def _accepted_rows(self, obj: GovernedObject, actions: dict[str, Any]) -> list[GovernanceEvent]:
        identity = obj.id
        canonical_actions = set(_RUNTIME_EFFECTS) | {
            n for n, a in actions.items() if a["kind"] in (ACTION_STATE, ACTION_LIFECYCLE)
        }
        positioned = self._runtime.accepted_history(identity)
        positioned_ids = {r.event_id for r in positioned}
        read_versions: list[int] = []
        for row in self._runtime.history(identity):
            positioned_row = row.event_id in positioned_ids
            candidate = (
                row.decision == Decision.ALLOW
                and row.event_type == f"object.{row.action}"
                and row.action in canonical_actions
            )
            if candidate and not positioned_row:
                raise ExportRefused(
                    EXPORT_ORDER_INDETERMINATE,
                    f"accepted {row.action!r} row {row.event_id} has no recorded position",
                    identity,
                )
            if positioned_row and not candidate:
                raise ExportRefused(
                    EXPORT_HISTORY_UNCLASSIFIABLE,
                    f"positioned row {row.event_id} ({row.decision.value} {row.event_type}) "
                    "is not an accepted canonical change",
                    identity,
                )
            if row.decision == Decision.ALLOW and not candidate:
                if row.action != "read" or row.event_type != "object.read":
                    raise ExportRefused(
                        EXPORT_HISTORY_UNCLASSIFIABLE,
                        f"accepted row {row.event_id} ({row.event_type}) is neither a "
                        "canonical change nor a read",
                        identity,
                    )
                read_versions.append(row.object_version)

        positions = [r.history_position for r in positioned]
        if positions != list(range(1, len(positioned) + 1)):
            raise ExportRefused(
                EXPORT_ORDER_INDETERMINATE, f"recorded positions are not 1..n: {positions}", identity
            )
        if not positioned or positioned[0].action != "create":
            raise ExportRefused(
                EXPORT_HISTORY_UNCLASSIFIABLE, "accepted history does not begin with create", identity
            )
        versions = [r.object_version for r in positioned]
        # Tokens are opaque: only reuse is refused. A restored history may
        # carry versions that do not increase in position order (B-REORDER).
        if len(set(versions)) != len(versions):
            raise ExportRefused(
                EXPORT_HISTORY_UNCLASSIFIABLE,
                f"an Object Version is reused within accepted history: {versions}",
                identity,
            )
        if versions[-1] != obj.version:
            raise ExportRefused(
                EXPORT_HISTORY_UNCLASSIFIABLE,
                f"current Object Version {obj.version} is not the last accepted version {versions[-1]}",
                identity,
            )
        stray = sorted(set(read_versions) - set(versions))
        if stray:
            raise ExportRefused(
                EXPORT_HISTORY_UNCLASSIFIABLE,
                f"read rows carry versions no accepted change produced: {stray}",
                identity,
            )
        return positioned

    def _entry(
        self,
        identity: str,
        row: GovernanceEvent,
        lc_field: str | None,
        actions: dict[str, Any],
        include_timestamps: bool,
    ) -> dict[str, Any]:
        if row.type_version is None:
            raise ExportRefused(
                EXPORT_HISTORY_UNCLASSIFIABLE,
                f"row {row.event_id} records no Type Version",
                identity,
            )
        effect = _RUNTIME_EFFECTS.get(row.action)
        if effect is None:
            kind = actions[row.action]["kind"]
            effect = "state_change" if kind == ACTION_STATE else "lifecycle_transition"
        entry: dict[str, Any] = {
            "position": row.history_position,
            "effect": effect,
            "type_identity": row.object_type,
            "type_version": row.type_version,
            "resulting_object_version": _token(row.object_version, identity),
        }
        details = row.details
        if effect == "create":
            if "data" not in details:
                raise ExportRefused(
                    EXPORT_HISTORY_UNCLASSIFIABLE, "create row records no post-image", identity
                )
            entry["post_image"] = _post_image(details["data"], row.new_state, lc_field)
            entry["relationships"] = [
                {"source": r["source_id"], "type": r["relation"], "target": r["target_id"]}
                for r in details.get("relationships", [])
            ]
        elif effect in ("state_change", "lifecycle_transition"):
            image = details.get("accepted_post_image")
            if image is None:
                raise ExportRefused(
                    EXPORT_HISTORY_UNCLASSIFIABLE,
                    f"{row.action!r} row {row.event_id} records no post-image",
                    identity,
                )
            entry["post_image"] = _post_image(image["data"], image["state"], lc_field)
        else:
            entry["relationship"] = {
                "source": identity,
                "type": details["relation"],
                "target": details["target_id"],
            }
        entry["actor"] = row.actor_id
        if include_timestamps and row.accepted_at_recorded:
            entry["accepted_at"] = row.timestamp.astimezone(UTC).isoformat()
        if details.get(ACTION_RECORDED, True):
            entry["action"] = row.action
        return entry


def _post_image(data: dict[str, Any], state: str | None, lc_field: str | None) -> dict[str, Any]:
    image = dict(data)
    if lc_field is not None:
        image[lc_field] = state
    return image


def _unique(items) -> list:
    out: list = []
    for item in items:
        if item not in out:
            out.append(item)
    return out
