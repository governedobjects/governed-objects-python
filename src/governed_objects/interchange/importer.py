# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Brafford Labs, LLC
"""Privileged new-target import of accepted Core history (EXPERIMENTAL).

EXPERIMENTAL. NON-NORMATIVE. NOT A GOVERNED OBJECT API. Part of the
interchange experiment.

The input is a runtime-neutral semantic structure (:class:`ImportBundle`),
not artifact JSON; decoding the provisional artifact is the adapter's job
(``governed_objects.interchange.artifact``). The importer:

* restores already-accepted history *as history*: no command is re-run, no
  authorization is re-evaluated for historical actors, no Object Version is
  generated, and no import event is appended to accepted history;
* resolves the declared governance reference against the local
  ``TypeScopedGovernance``; a match selects local governance and grants
  nothing;
* checks the receiver-specific constraints of this runtime (identity absent,
  exact Type Identity and Type Version, representable Object Version tokens,
  native encodability of each history effect, representable evidence) and
  folds the history natively so the stored snapshot is the one the history
  produces;
* writes everything in one SQLite transaction
  (:meth:`SQLiteStore.restore_imported`). Either the whole bundle is restored
  or the store is unchanged.

Import provenance is Operational Metadata in ``interchange_imports``; it is
never part of accepted Canonical History.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Mapping
from uuid import uuid4

from governed_objects.domain.models import Actor, Decision, GovernanceEvent, GovernedObject
from governed_objects.interchange import (
    GOVERNANCE_REFERENCE_MODEL,
    HISTORY_COMPLETENESS,
    HISTORY_POSITION_MODEL,
    IMPORT_SPEC_REVISIONS,
)
from governed_objects.interchange.types import (
    ACTION_LIFECYCLE,
    ACTION_STATE,
    NO_LIFECYCLE_STATE,
    data_problems,
    lifecycle_field,
    portable_type_of,
)
from governed_objects.relationships.canonical import canonical_relationship

if TYPE_CHECKING:
    from governed_objects.domain.clock import Clock
    from governed_objects.interchange.governance import TypeScopedGovernance
    from governed_objects.persistence.sqlite import SQLiteStore
    from governed_objects.types.registry import TypeRegistry

IMPORT_CAPABILITY = "interchange.import"
IMPORT_EXPERIMENT_REVISION = "governed-objects-import-experiment/1"

# Native actor_type recorded on restored rows: the actor id is historical
# evidence from the source, not a principal of this runtime.
HISTORICAL_ACTOR_TYPE = "historical"
# Stored in the NOT NULL timestamp column when the artifact carries no
# acceptance instant; accepted_at_recorded is False on such rows.
UNRECORDED_ACCEPTED_AT = datetime(1, 1, 1, tzinfo=UTC)
# Provenance every history entry must carry (NOT NULL native columns).
REQUIRED_PROVENANCE = ("actor",)
# False in a restored row's details when its native action encodes the effect but
# was not carried as action evidence; export then carries no action for it.
ACTION_RECORDED = "action_recorded"

EFFECT_CREATE = "create"
EFFECT_STATE_CHANGE = "state_change"
EFFECT_LIFECYCLE_TRANSITION = "lifecycle_transition"
EFFECT_RELATIONSHIP_ADD = "relationship_add"
EFFECT_RELATIONSHIP_REMOVE = "relationship_remove"
POST_IMAGE_EFFECTS = (EFFECT_CREATE, EFFECT_STATE_CHANGE, EFFECT_LIFECYCLE_TRANSITION)

# Refusal codes: the experimental taxonomy of the interchange repository,
# plus IDENTITY_ALREADY_EXISTS for new-target transfer and AUTHORIZATION_DENIED.
AUTHORIZATION_DENIED = "AUTHORIZATION_DENIED"
IDENTITY_ALREADY_EXISTS = "IDENTITY_ALREADY_EXISTS"
UNSUPPORTED_SPEC_REVISION = "UNSUPPORTED_SPEC_REVISION"
MALFORMED_ARTIFACT = "MALFORMED_ARTIFACT"
DUPLICATE_IDENTITY_IN_BUNDLE = "DUPLICATE_IDENTITY_IN_BUNDLE"
UNSUPPORTED_TYPE = "UNSUPPORTED_TYPE"
UNSUPPORTED_TYPE_VERSION = "UNSUPPORTED_TYPE_VERSION"
STATE_NOT_VALID_FOR_TYPE = "STATE_NOT_VALID_FOR_TYPE"
HISTORY_NOT_VALID_FOR_TYPE = "HISTORY_NOT_VALID_FOR_TYPE"
DUPLICATE_RELATIONSHIP = "DUPLICATE_RELATIONSHIP"
MULTISET_HISTORY = "MULTISET_HISTORY"
HISTORY_INCOMPLETE = "HISTORY_INCOMPLETE"
HISTORY_INCONSISTENT = "HISTORY_INCONSISTENT"
HISTORY_SNAPSHOT_MISMATCH = "HISTORY_SNAPSHOT_MISMATCH"
VERSION_TOKEN_REUSE = "VERSION_TOKEN_REUSE"
UNSUPPORTED_OBJECT_VERSION_REPRESENTATION = "UNSUPPORTED_OBJECT_VERSION_REPRESENTATION"
GOVERNANCE_REFERENCE_MISSING = "GOVERNANCE_REFERENCE_MISSING"
UNRESOLVED_GOVERNANCE_REFERENCE = "UNRESOLVED_GOVERNANCE_REFERENCE"
UNSUPPORTED_SEMANTIC_FEATURE = "UNSUPPORTED_SEMANTIC_FEATURE"
BUNDLE_CLOSURE_VIOLATED = "BUNDLE_CLOSURE_VIOLATED"

# Native Object Versions are SQLite INTEGER (signed 64-bit) values issued from
# 1 upward. A portable token is representable only if it is the canonical
# base-10 form of such a value, so parsing and re-rendering reproduce it.
NATIVE_TOKEN_GRAMMAR = r"^[1-9][0-9]*$ with value <= 9223372036854775807"
_TOKEN = re.compile(r"^[1-9][0-9]*$")
_NATIVE_VERSION_MAX = 2**63 - 1

_INSTANT = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})[Tt](\d{2}):(\d{2}):(\d{2})(\.\d+)?([Zz]|[+-]\d{2}:\d{2})$"
)


class ImportRefused(Exception):
    def __init__(
        self, code: str, message: str, identity: str | None = None, position: int | None = None
    ) -> None:
        where = ".".join(
            p for p in (identity, f"history[position={position}]" if position else None) if p
        )
        super().__init__(f"{code}: {where + ': ' if where else ''}{message}")
        self.code = code
        self.message = message
        self.identity = identity
        self.position = position


@dataclass(frozen=True)
class ImportRelationship:
    source: str
    type: str
    target: str
    # OPTIONAL EVIDENCE (target-type hint, metadata); not retained natively.
    has_evidence: bool = False

    def key(self) -> tuple[str, str, str]:
        return (self.source, self.type, self.target)


@dataclass(frozen=True)
class ImportEvidence:
    actor: str | None = None
    accepted_at: str | None = None
    action: str | None = None
    source_event_id: str | None = None


@dataclass(frozen=True)
class ImportEntry:
    position: int
    effect: str
    type_identity: str
    type_version: str
    resulting_object_version: str
    post_image: Mapping[str, Any] | None = None
    baseline_relationships: tuple[ImportRelationship, ...] | None = None
    relationship: ImportRelationship | None = None
    evidence: ImportEvidence = field(default_factory=ImportEvidence)


@dataclass(frozen=True)
class ImportObject:
    canonical_identity: str
    type_identity: str
    type_version: str
    governance_reference: str | None
    accepted_state: Mapping[str, Any]
    relationships: tuple[ImportRelationship, ...]
    current_object_version: str
    history: tuple[ImportEntry, ...]


@dataclass(frozen=True)
class ImportBundle:
    """Semantic content of a portable bundle, independent of its encoding.

    The declared models are the experiment's semantic feature set; the
    importer refuses any it does not implement."""

    objects: tuple[ImportObject, ...]
    closed: bool
    spec_revision: str | None
    history_completeness: str
    history_position_model: str
    governance_reference_model: str
    profiles: tuple[Any, ...] = ()
    source_implementation: str | None = None


@dataclass(frozen=True)
class ImportProvenance:
    """Operational Metadata about the transfer. Never semantic."""

    artifact_sha256: str | None = None
    source_implementation: str | None = None
    adapter: str | None = None


@dataclass(frozen=True)
class ImportResult:
    import_id: str
    identities: tuple[str, ...]
    # Optional evidence this runtime could not retain or had to derive. Only
    # evidence the frozen comparator treats as informational or excluded can
    # appear here; unrepresentable actor or accepted_at evidence refuses.
    evidence_notes: tuple[str, ...]


@dataclass
class _Staged:
    obj: GovernedObject
    events: list[GovernanceEvent]
    relationships: list


def native_version(token: Any, identity: str, position: int | None = None) -> int:
    """Parse a portable token into the native Object Version, refusing any
    token whose native value would not re-render to the same string."""
    if not isinstance(token, str) or not _TOKEN.match(token):
        raise ImportRefused(
            UNSUPPORTED_OBJECT_VERSION_REPRESENTATION,
            f"token {token!r} is not a canonical positive base-10 integer; the native Object "
            "Version is an integer and the token would not survive exactly",
            identity,
            position,
        )
    value = int(token)
    if value > _NATIVE_VERSION_MAX:
        raise ImportRefused(
            UNSUPPORTED_OBJECT_VERSION_REPRESENTATION,
            f"token {token!r} exceeds the native signed 64-bit Object Version range",
            identity,
            position,
        )
    if str(value) != token:  # defensive: the grammar already guarantees this
        raise ImportRefused(
            UNSUPPORTED_OBJECT_VERSION_REPRESENTATION, f"token {token!r} does not round-trip",
            identity, position,
        )
    return value


def native_accepted_at(text: str, identity: str, position: int) -> datetime:
    """Parse RFC 3339 evidence into a native UTC datetime, refusing any
    instant this runtime cannot hold exactly (the native timestamp has
    microsecond precision)."""
    m = _INSTANT.match(text) if isinstance(text, str) else None
    if not m:
        raise ImportRefused(MALFORMED_ARTIFACT, "accepted_at is not an RFC 3339 instant", identity, position)
    year, month, day, hour, minute, second = (int(g) for g in m.groups()[:6])
    frac, zone = m.group(7), m.group(8)
    try:
        if zone in ("Z", "z"):
            tz = UTC
        else:
            sign = 1 if zone[0] == "+" else -1
            tz = timezone(sign * timedelta(hours=int(zone[1:3]), minutes=int(zone[4:6])))
    except (ValueError, OverflowError):
        raise ImportRefused(MALFORMED_ARTIFACT, f"accepted_at {text!r} is not a valid instant", identity, position) from None
    digits = frac[1:] if frac else ""
    if len(digits.rstrip("0")) > 6:
        raise ImportRefused(
            UNSUPPORTED_SEMANTIC_FEATURE,
            f"accepted_at {text!r} has sub-microsecond precision; the native timestamp would "
            "not hold this instant exactly (evidence is never truncated)",
            identity,
            position,
        )
    micros = int((digits + "000000")[:6]) if digits else 0
    try:
        local = datetime(year, month, day, hour, minute, second, micros, tzinfo=tz)
        instant = local.astimezone(UTC)
    except (ValueError, OverflowError):
        raise ImportRefused(MALFORMED_ARTIFACT, f"accepted_at {text!r} is not a valid instant", identity, position)
    if instant == UNRECORDED_ACCEPTED_AT:
        raise ImportRefused(
            UNSUPPORTED_SEMANTIC_FEATURE,
            f"accepted_at {text!r} coincides with the native 'not recorded' marker",
            identity,
            position,
        )
    return instant


class InterchangeImporter:
    def __init__(
        self,
        store: SQLiteStore,
        types: TypeRegistry,
        governance: TypeScopedGovernance | None,
        clock: Clock,
    ) -> None:
        self._store = store
        self._types = types
        self._governance = governance
        self._clock = clock

    def import_bundle(
        self, bundle: ImportBundle, *, actor: Actor, provenance: ImportProvenance | None = None
    ) -> ImportResult:
        if IMPORT_CAPABILITY not in actor.capabilities:
            raise ImportRefused(AUTHORIZATION_DENIED, f"actor {actor.id!r} holds no import grant")
        self._check_declarations(bundle)
        if not bundle.objects:
            raise ImportRefused(MALFORMED_ARTIFACT, "bundle contains no objects")
        identities = [o.canonical_identity for o in bundle.objects]
        for ident in identities:
            if identities.count(ident) > 1:
                raise ImportRefused(DUPLICATE_IDENTITY_IN_BUNDLE, "appears more than once", ident)
        if bundle.closed:
            present = set(identities)
            for obj in bundle.objects:
                for r in obj.relationships:
                    if r.target not in present:
                        raise ImportRefused(
                            BUNDLE_CLOSURE_VIOLATED,
                            f"bundle declares closure but target {r.target!r} is absent",
                            obj.canonical_identity,
                        )
        existing = self._store.identities_present(identities)
        if existing:
            raise ImportRefused(
                IDENTITY_ALREADY_EXISTS,
                f"identities already exist in this runtime: {existing}; new-target import "
                "never overwrites, merges, or skips",
                existing[0],
            )

        notes: list[str] = []
        staged = [self._stage(obj, notes) for obj in bundle.objects]

        import_id = str(uuid4())
        record = {
            "import_id": import_id,
            "imported_at": self._clock.now().astimezone(UTC).isoformat(),
            "importing_actor": actor.id,
            "identities": identities,
            "source_implementation": (provenance.source_implementation if provenance else None)
            or bundle.source_implementation,
            "artifact_sha256": provenance.artifact_sha256 if provenance else None,
            "adapter": provenance.adapter if provenance else None,
            "experiment_revision": IMPORT_EXPERIMENT_REVISION,
            "evidence_notes": notes,
        }
        from governed_objects.persistence.sqlite import IdentityAlreadyExists

        try:
            self._store.restore_imported(
                [s.obj for s in staged],
                [e for s in staged for e in s.events],
                [r for s in staged for r in s.relationships],
                record,
            )
        except IdentityAlreadyExists as exc:
            raise ImportRefused(
                IDENTITY_ALREADY_EXISTS, f"identities already exist in this runtime: {exc.identities}",
                exc.identities[0],
            ) from None
        return ImportResult(import_id=import_id, identities=tuple(identities), evidence_notes=tuple(notes))

    # --- checks ------------------------------------------------------------

    @staticmethod
    def _check_declarations(bundle: ImportBundle) -> None:
        if bundle.spec_revision not in IMPORT_SPEC_REVISIONS:
            raise ImportRefused(
                UNSUPPORTED_SPEC_REVISION, f"specification revision {bundle.spec_revision!r} is not supported"
            )
        if bundle.history_completeness != HISTORY_COMPLETENESS:
            raise ImportRefused(
                HISTORY_INCOMPLETE,
                f"history completeness {bundle.history_completeness!r}; only complete history is imported",
            )
        if bundle.history_position_model != HISTORY_POSITION_MODEL:
            raise ImportRefused(
                MALFORMED_ARTIFACT, f"history position model {bundle.history_position_model!r} is not supported"
            )
        if bundle.governance_reference_model != GOVERNANCE_REFERENCE_MODEL:
            raise ImportRefused(
                UNSUPPORTED_SEMANTIC_FEATURE,
                f"governance reference model {bundle.governance_reference_model!r} is not supported",
            )
        if bundle.profiles:
            raise ImportRefused(UNSUPPORTED_SEMANTIC_FEATURE, "profile data is not supported")

    def _portable(self, obj: ImportObject) -> Mapping[str, Any]:
        ident = obj.canonical_identity
        try:
            type_def = self._types.get(obj.type_identity)
        except KeyError:
            raise ImportRefused(UNSUPPORTED_TYPE, f"Type Identity {obj.type_identity!r} is not configured", ident)
        portable = portable_type_of(type_def)
        if portable is None:
            raise ImportRefused(UNSUPPORTED_TYPE, f"{obj.type_identity!r} is not a portable Core type", ident)
        if type_def.type_version != obj.type_version:
            raise ImportRefused(
                UNSUPPORTED_TYPE_VERSION,
                f"{obj.type_identity}@{obj.type_version} is not supported; this runtime is configured "
                f"with @{type_def.type_version} (never upcast, downcast, or migrated)",
                ident,
            )
        for e in obj.history:
            if e.type_identity != obj.type_identity:
                raise ImportRefused(
                    HISTORY_INCONSISTENT, f"entry Type Identity {e.type_identity!r} differs from the object's",
                    ident, e.position,
                )
            if e.type_version != obj.type_version:
                raise ImportRefused(
                    HISTORY_INCONSISTENT, f"entry Type Version {e.type_version!r} differs from the object's",
                    ident, e.position,
                )
        return portable

    def _check_governance(self, obj: ImportObject) -> str:
        ident = obj.canonical_identity
        if not obj.governance_reference:
            raise ImportRefused(GOVERNANCE_REFERENCE_MISSING, "object carries no governance reference", ident)
        local = (
            self._governance.reference_for(obj.type_identity, obj.type_version)
            if self._governance is not None
            else None
        )
        if local != obj.governance_reference:
            raise ImportRefused(
                UNRESOLVED_GOVERNANCE_REFERENCE,
                f"{obj.governance_reference!r} does not resolve for {obj.type_identity}@{obj.type_version} "
                f"under local governance {getattr(self._governance, 'name', None)!r}",
                ident,
            )
        return local

    @staticmethod
    def _state_problems(portable: Mapping[str, Any], state: Mapping[str, Any]) -> list[str]:
        lc = lifecycle_field(portable)
        problems: list[str] = []
        if lc is not None:
            if lc not in state:
                problems.append(f"missing lifecycle field {lc!r}")
            elif not isinstance(state[lc], str) or state[lc] not in portable["lifecycle"]["states"]:
                problems.append(f"lifecycle value {state[lc]!r} is not a declared state")
        problems += data_problems(portable, {k: v for k, v in state.items() if k != lc}, partial=False)
        return problems

    # --- staging -----------------------------------------------------------

    def _stage(self, obj: ImportObject, notes: list[str]) -> _Staged:
        ident = obj.canonical_identity
        portable = self._portable(obj)
        reference = self._check_governance(obj)
        lc = lifecycle_field(portable)
        actions = portable.get("actions", {})

        for where, state, position in [("accepted_state", obj.accepted_state, None)] + [
            ("post_image", e.post_image, e.position) for e in obj.history if e.post_image is not None
        ]:
            problems = self._state_problems(portable, state)
            if problems:
                raise ImportRefused(STATE_NOT_VALID_FOR_TYPE, f"{where}: {'; '.join(problems)}", ident, position)

        rel_lists = [("relationships", obj.relationships, None)] + [
            ("baseline relationships", e.baseline_relationships, e.position)
            for e in obj.history
            if e.baseline_relationships is not None
        ]
        for where, rels, position in rel_lists:
            keys = [r.key() for r in rels]
            for r in rels:
                if r.source != ident:
                    raise ImportRefused(MALFORMED_ARTIFACT, f"{where}: triple source {r.source!r} is not the object", ident, position)
            for k in keys:
                if keys.count(k) > 1:
                    raise ImportRefused(DUPLICATE_RELATIONSHIP, f"{where}: triple {k} listed more than once", ident, position)
        for e in obj.history:
            if e.relationship is not None and e.relationship.source != ident:
                raise ImportRefused(MALFORMED_ARTIFACT, "triple source is not the object", ident, e.position)
        if any(r.has_evidence for _, rels, _ in rel_lists for r in rels) or any(
            e.relationship is not None and e.relationship.has_evidence for e in obj.history
        ):
            notes.append(f"{ident}: relationship target-type/metadata evidence not retained (informational)")

        current = native_version(obj.current_object_version, ident)
        natives: dict[int, str] = {}
        for e in obj.history:
            value = native_version(e.resulting_object_version, ident, e.position)
            if value in natives:
                raise ImportRefused(
                    VERSION_TOKEN_REUSE,
                    f"resulting Object Version {e.resulting_object_version!r} collides with "
                    f"{natives[value]!r} natively; tokens are never merged or repaired",
                    ident,
                    e.position,
                )
            natives[value] = e.resulting_object_version

        if not obj.history:
            raise ImportRefused(HISTORY_INCOMPLETE, "history is empty", ident)
        positions = sorted(e.position for e in obj.history)
        if positions != list(range(1, len(obj.history) + 1)):
            raise ImportRefused(HISTORY_INCONSISTENT, f"positions are not exactly 1..n: {positions}", ident)
        ordered = sorted(obj.history, key=lambda e: e.position)
        if ordered[0].effect != EFFECT_CREATE:
            raise ImportRefused(HISTORY_INCOMPLETE, "position 1 is not a creation baseline", ident, 1)
        if ordered[-1].resulting_object_version != obj.current_object_version:
            raise ImportRefused(
                HISTORY_INCONSISTENT,
                f"current Object Version {obj.current_object_version!r} is not the last entry's "
                f"{ordered[-1].resulting_object_version!r}",
                ident,
            )

        events: list[GovernanceEvent] = []
        state: dict[str, Any] = {}
        rels: set[tuple[str, str, str]] = set()
        for e in ordered:
            for item in REQUIRED_PROVENANCE:
                if not getattr(e.evidence, item):
                    raise ImportRefused(
                        UNSUPPORTED_SEMANTIC_FEATURE,
                        f"entry carries no {item} evidence; the native history row requires one",
                        ident, e.position,
                    )
            before_lc = state.get(lc) if lc else NO_LIFECYCLE_STATE
            if e.effect == EFFECT_CREATE:
                if e.position != 1:
                    raise ImportRefused(HISTORY_INCONSISTENT, "creation entry after position 1", ident, e.position)
                state = dict(e.post_image or {})
                rels = {r.key() for r in e.baseline_relationships or ()}
                action = "create"
            elif e.effect == EFFECT_STATE_CHANGE:
                new = dict(e.post_image or {})
                if lc and new.get(lc) != state.get(lc):
                    raise ImportRefused(
                        HISTORY_NOT_VALID_FOR_TYPE,
                        "state_change alters the lifecycle; lifecycle changes must be lifecycle_transition",
                        ident, e.position,
                    )
                action = self._native_action(e, actions, ACTION_STATE, None, None, ident, notes)
                state = new
            elif e.effect == EFFECT_LIFECYCLE_TRANSITION:
                new = dict(e.post_image or {})
                if lc is None:
                    raise ImportRefused(
                        HISTORY_NOT_VALID_FOR_TYPE, f"{obj.type_identity} declares no lifecycle", ident, e.position
                    )
                before, after = state.get(lc), new.get(lc)
                if [before, after] not in [list(t) for t in portable["lifecycle"]["transitions"]]:
                    raise ImportRefused(
                        HISTORY_NOT_VALID_FOR_TYPE,
                        f"lifecycle transition {before!r} -> {after!r} is not permitted by "
                        f"{obj.type_identity}@{obj.type_version}",
                        ident, e.position,
                    )
                action = self._native_action(e, actions, ACTION_LIFECYCLE, before, after, ident, notes)
                state = new
            elif e.effect == EFFECT_RELATIONSHIP_ADD:
                key = e.relationship.key()
                if key in rels:
                    raise ImportRefused(MULTISET_HISTORY, f"addition of already-present triple {key}", ident, e.position)
                rels.add(key)
                action = "add_relationship"
            elif e.effect == EFFECT_RELATIONSHIP_REMOVE:
                key = e.relationship.key()
                if key not in rels:
                    raise ImportRefused(HISTORY_INCONSISTENT, f"removal of absent triple {key}", ident, e.position)
                rels.discard(key)
                action = "remove_relationship"
            else:
                raise ImportRefused(MALFORMED_ARTIFACT, f"unknown effect {e.effect!r}", ident, e.position)
            if e.evidence.action is not None and e.evidence.action != action:
                notes.append(
                    f"{ident}@{e.position}: action evidence {e.evidence.action!r} not retained; "
                    f"native action {action!r} encodes the effect (informational)"
                )
            elif e.evidence.action is None and e.effect in (EFFECT_STATE_CHANGE, EFFECT_LIFECYCLE_TRANSITION):
                notes.append(f"{ident}@{e.position}: no action evidence; native action {action!r} derived from the effect")
            if e.evidence.source_event_id is not None:
                notes.append(f"{ident}@{e.position}: source_event_id evidence not retained (excluded dimension)")
            events.append(self._row(obj, e, action, before_lc, state, lc, reference))

        declared = dict(obj.accepted_state)
        if state != declared or any(type(state[k]) is not type(declared[k]) for k in state):
            raise ImportRefused(HISTORY_SNAPSHOT_MISMATCH, "accepted_state is not the state the history produces", ident)
        if rels != {r.key() for r in obj.relationships}:
            raise ImportRefused(
                HISTORY_SNAPSHOT_MISMATCH, "relationship set is not the set the history produces", ident
            )

        stored = GovernedObject(
            id=ident,
            type=obj.type_identity,
            type_version=obj.type_version,
            version=current,
            state=declared[lc] if lc else NO_LIFECYCLE_STATE,
            data={k: v for k, v in declared.items() if k != lc},
            governance_reference=reference,
        )
        relationships = [canonical_relationship(s, t, g) for s, t, g in sorted(rels)]
        return _Staged(stored, events, relationships)

    @staticmethod
    def _native_action(e, actions, kind, before, after, ident, notes) -> str:
        """The native row encodes the effect by action name. Choose the
        declared action the evidence names when it fits the effect, else the
        unique declared action that fits."""
        def fits(name: str) -> bool:
            spec = actions.get(name)
            if spec is None or spec["kind"] != kind:
                return False
            return kind == ACTION_STATE or (before in spec["from"] and spec["to"] == after)

        if e.evidence.action is not None and fits(e.evidence.action):
            return e.evidence.action
        candidates = sorted(n for n in actions if fits(n))
        if len(candidates) != 1:
            raise ImportRefused(
                UNSUPPORTED_SEMANTIC_FEATURE,
                f"{e.effect} {before!r} -> {after!r} has {len(candidates)} native encodings "
                f"{candidates} and the action evidence does not select one",
                ident, e.position,
            )
        return candidates[0]

    def _row(self, obj, e: ImportEntry, action, before_lc, state, lc, reference) -> GovernanceEvent:
        after_lc = state.get(lc) if lc else NO_LIFECYCLE_STATE
        data = {k: v for k, v in state.items() if k != lc}
        details: dict[str, Any] = {"validation_checks": []}
        if e.effect == EFFECT_CREATE:
            details["data"] = data
            if e.baseline_relationships:
                details["relationships"] = [
                    {"source_id": r.source, "relation": r.type, "target_id": r.target, "target_type": None}
                    for r in e.baseline_relationships
                ]
            details["governance_reference"] = reference
        elif e.effect in (EFFECT_STATE_CHANGE, EFFECT_LIFECYCLE_TRANSITION):
            details["accepted_post_image"] = {"data": data, "state": after_lc}
        elif e.effect == EFFECT_RELATIONSHIP_ADD:
            details.update({"relation": e.relationship.type, "target_id": e.relationship.target, "target_type": None})
        else:
            details.update({"relation": e.relationship.type, "target_id": e.relationship.target})
        if e.evidence.action != action:
            details[ACTION_RECORDED] = False
        recorded = e.evidence.accepted_at is not None
        timestamp = (
            native_accepted_at(e.evidence.accepted_at, obj.canonical_identity, e.position)
            if recorded
            else UNRECORDED_ACCEPTED_AT
        )
        return GovernanceEvent(
            timestamp=timestamp,
            object_id=obj.canonical_identity,
            object_type=obj.type_identity,
            type_version=e.type_version,
            actor_id=e.evidence.actor,
            actor_type=HISTORICAL_ACTOR_TYPE,
            action=action,
            decision=Decision.ALLOW,
            event_type=f"object.{action}",
            previous_state=None if e.effect == EFFECT_CREATE else before_lc,
            new_state=after_lc,
            reason="",
            object_version=native_version(e.resulting_object_version, obj.canonical_identity, e.position),
            details=details,
            history_position=e.position,
            accepted_at_recorded=recorded,
        )
