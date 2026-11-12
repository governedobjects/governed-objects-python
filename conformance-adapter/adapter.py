#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Brafford Labs, LLC
"""Conformance adapter for the Python runtime. NON-NORMATIVE test infrastructure.

See ADAPTER-AUDIT.md (adapter notes). Two channels are kept apart:

- ``caller_result`` is the exact ``CommandResult`` (or import result/refusal)
  the runtime returned to the requester, as ``model_dump(mode="json")``, with
  nothing removed, added, or renamed.
- ``semantic_view`` / ``canonical_history`` are administrative observations
  (stored object, canonical relationships, ``accepted_history``) made after the
  step. They are never caller-visible and are used only to check canonical
  effects.
"""

from __future__ import annotations

import dataclasses
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

# The runtime under test is the one that contains this directory (governed-objects-python); an
# explicit CONFORMANCE_PYTHON_RUNTIME_REPO overrides it.
_GOVERNED_REPO = Path(os.environ.get("CONFORMANCE_PYTHON_RUNTIME_REPO") or Path(__file__).resolve().parents[1])
_GOVERNED_SRC = _GOVERNED_REPO / "src"
sys.path.insert(0, str(_GOVERNED_SRC))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import governed_objects  # noqa: E402
from governed_objects.applications.registry import ApplicationRegistry  # noqa: E402
from governed_objects.providers.registry import ProviderRegistry  # noqa: E402
from governed_objects.types.extensions import TypeExtensionRegistry  # noqa: E402
from governed_objects.domain.command import (  # noqa: E402
    AddRelationship,
    Command,
    CreateObject,
    ReadObject,
    RemoveRelationship,
    TransitionState,
)
from governed_objects.domain.command_context import CommandContext  # noqa: E402
from governed_objects.domain.models import Actor, ActorType, Decision  # noqa: E402
from governed_objects.interchange import (  # noqa: E402
    GOVERNANCE_REFERENCE_MODEL,
    HISTORY_COMPLETENESS,
    HISTORY_POSITION_MODEL,
    SPEC_REVISION,
)
from governed_objects.interchange.governance import TypeScopedGovernance  # noqa: E402
from governed_objects.interchange.importer import (  # noqa: E402
    AUTHORIZATION_DENIED as IMPORT_AUTHORIZATION_DENIED,
    IMPORT_CAPABILITY,
    NATIVE_TOKEN_GRAMMAR,
    ImportBundle,
    ImportEntry,
    ImportEvidence,
    ImportObject,
    ImportProvenance,
    ImportRefused,
    ImportRelationship,
)
from governed_objects.interchange.types import (  # noqa: E402
    PortableTypeAdapter,
    build_type_definition,
)
from governed_objects.persistence.sqlite import INT64_MAX, SQLiteStore  # noqa: E402
from governed_objects.policy.reasons import DenyReason  # noqa: E402
from governed_objects.runtime.runtime import GovernedObjectRuntime  # noqa: E402
from governed_objects.types.registry import TypeRegistry  # noqa: E402

from conformance_types import ConformanceFixtureAdapter  # noqa: E402
import fixture_types as ft  # noqa: E402

WORK_ITEM = "conformance.work-item"
RECORD = "conformance.record"
GOVERNANCE_REF = "conformance-governance/1"


class RequestError(Exception):
    pass


def _dump(value: Any) -> Any:
    if value is None:
        return None
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if dataclasses.is_dataclass(value):
        return json.loads(json.dumps(dataclasses.asdict(value), default=str))
    return value


def _expected_version(req: dict[str, Any]) -> int | None:
    if "expected_version" not in req or req["expected_version"] is None:
        return None
    raw = req["expected_version"]
    if isinstance(raw, bool):
        raise RequestError(f"expected_version {raw!r} is not a native Object Version")
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str) and raw.isdigit():
        return int(raw)
    raise RequestError(f"expected_version {raw!r} is not a native Object Version")


def _implementation_version() -> str:
    """Self-declared version of the implementation under test (the runtime package version)."""
    return os.environ.get("CONFORMANCE_IMPLEMENTATION_VERSION") or str(getattr(governed_objects, "__version__", "unknown"))


class AdapterSession:
    def __init__(self) -> None:
        self._tmpdir: tempfile.TemporaryDirectory | None = None
        self._fx: dict[str, Any] | None = None
        self._fx_sha: str | None = None
        self.reset()

    # --- lifecycle ---------------------------------------------------------

    def reset(self) -> None:
        """Discard objects and revocations; received fixture definitions are kept."""
        if self._tmpdir is not None:
            self._close_store()
            self._tmpdir.cleanup()
        self._tmpdir = tempfile.TemporaryDirectory()
        self._db = Path(self._tmpdir.name) / "conformance.db"
        self._revoked: set[str] = set()
        self._start()

    def _close_store(self) -> None:
        close = getattr(self._store, "close", None)
        if callable(close):
            close()

    def _spec(self, fixture_id: str) -> dict[str, Any]:
        specs = (self._fx or {}).get("types", {})
        if fixture_id not in specs:
            raise RequestError(f"type {fixture_id!r} is not a suite fixture (define_fixture_types not received?)")
        return specs[fixture_id]

    def _start(self) -> None:
        """(Re)build the runtime over the session store from the suite's fixture definitions."""
        self._types = TypeRegistry()
        self._kind_of: dict[str, str] = {}
        self._fixture_of: dict[str, str] = {}
        self._effects: dict[str, dict[str, str]] = {}
        rules: dict[str, dict[str, list[str]]] = {}
        updates: dict[str, dict[str, str]] = {}
        governance: dict[tuple[str, str], str] = {}
        for fixture_id, spec in ((self._fx or {}).get("types") or {}).items():
            kind = ft.native_kind(fixture_id)
            self._kind_of[fixture_id] = kind
            self._fixture_of[kind] = fixture_id
            self._effects[kind] = ft.effects(spec)
            if fixture_id in ft.PORTABLE_IDS:
                self._types.register(
                    build_type_definition(ft.portable_description(fixture_id, spec), ft.portable_requirements())
                )
                governance[(fixture_id, spec["version"])] = GOVERNANCE_REF
            else:
                self._types.register(ft.legacy_definition(fixture_id, spec))
                rules[kind] = {"create": ft.blank_rules(spec, "create"), "update": ft.blank_rules(spec, "update")}
                updates[kind] = {ft.update_action(f): f for f in ft.data_fields(spec)}
        self._store = SQLiteStore(self._db)
        self._runtime = GovernedObjectRuntime(
            self._store,
            type_registry=self._types,
            adapters=[ConformanceFixtureAdapter(rules, updates), PortableTypeAdapter(self._types)],
            extension_registry=TypeExtensionRegistry(),
            providers=ProviderRegistry(),
            application_registry=ApplicationRegistry(),
            governance=TypeScopedGovernance("conformance", governance),
        )
        self._actors = {}
        for name, perms in ((self._fx or {}).get("actors") or {}).items():
            caps = [] if name in self._revoked else [
                IMPORT_CAPABILITY if p == "import" else ft.PERMISSION_CAPABILITY[p] for p in perms
            ]
            # mutate covers update and both relationship permissions in this runtime
            self._actors[name] = Actor(id=name, type=ActorType.SERVICE, capabilities=list(dict.fromkeys(caps)))

    def define_fixture_types(self, req: dict[str, Any]) -> dict[str, Any]:
        fixtures = req.get("fixtures")
        if not isinstance(fixtures, dict) or not fixtures.get("types"):
            raise RequestError("define_fixture_types requires the suite fixture definitions")
        self._fx = fixtures
        self._fx_sha = req.get("fixtures_sha256")
        self._close_store()
        self._start()
        return {
            "outcome": "ACCEPTED",
            "unmappable": False,
            "fixtures_sha256": self._fx_sha,
            "types_defined": sorted(self._kind_of),
        }

    def restart(self, req: dict[str, Any]) -> dict[str, Any]:
        for name in req.get("revoke") or []:
            if name not in self._actors:
                raise RequestError(f"unknown actor {name!r}")
            self._revoked.add(name)
        self._close_store()
        self._start()
        return {"outcome": "ACCEPTED", "unmappable": False}

    def capabilities(self) -> dict[str, Any]:
        return {
            "protocol": "draft2.2",
            "implementation": {"name": "governed-objects-python", "version": _implementation_version()},
            "spec_version": "v0alpha1",
            "spec_revision": "draft2.2",
            "core_claimed": True,
            "profiles_claimed": [],
            "replay": True,
            "replay_types": [WORK_ITEM, RECORD],
            "mutable_objects": True,
            "expected_version_required": False,
            "unconditional_mutation": True,
            "relationship_add": True,
            "relationship_remove": True,
            "type_version_exposed": True,
            "semantic_portability_view": True,
            "replay_baseline": "stored_snapshot_plus_allow_events",
            "caller_result": "exact CommandResult.model_dump(mode='json')",
            "observer_channel": "get_object(raw=True), canonical_relationships, accepted_history (administrative)",
            "actors": sorted(self._actors),
            "restart": True,
            "governance_reconfiguration": True,
            "harness_clock": False,
            "restoration": True,
            "restoration_api": "GovernedObjectRuntime.import_interchange_bundle",
            "restorable_types": [RECORD],
            "create_with_relationships": True,
            "operational_metadata_ops": ["detach_provider"],
            "version_token_grammar": NATIVE_TOKEN_GRAMMAR,
            "version_request_encoding": "decimal-integer",
            "object_version_ceiling": str(INT64_MAX),
            "declared_refusals": [DenyReason.OBJECT_VERSION_SPACE_EXHAUSTED.value, DenyReason.ADAPTER_RESULT_INVALID.value],
            "concurrent_invocation": False,
            "build": {
                "python": sys.version.split()[0],
                "runtime_repo": str(_GOVERNED_REPO),
                "governed_objects_module": governed_objects.__file__,
            },
            "notes": [
                "GovernedObjectRuntime(clock=...) does not govern accepted GovernanceEvent timestamps (datetime.now(UTC)); no history clock hook",
            ],
        }

    # --- helpers -------------------------------------------------------------

    def _actor(self, name: str) -> Actor:
        if name not in self._actors:
            raise RequestError(f"unknown actor {name!r}")
        return self._actors[name]

    @staticmethod
    def _classify(result) -> tuple[str, bool]:
        """The runtime's own classification, never inferred from reasons or
        checks. A non-accepted result the runtime left uncategorized is
        reported as UNCLASSIFIED: it executed, and carries no outcome class."""
        if result.outcome_category:
            return str(result.outcome_category), False
        if result.decision == Decision.ALLOW:
            return "ACCEPTED", False
        return "UNCLASSIFIED", False

    def _effect(self, native_type: str, action: str) -> str:
        return self._effects.get(native_type, {}).get(action, action)

    def _history_entry(self, evt) -> dict[str, Any]:
        details = evt.details or {}
        entry = {
            "effect": self._effect(evt.object_type, evt.action),
            "canonical_identity": evt.object_id,
            "type_identity": self._fixture_of.get(evt.object_type, evt.object_type),
            "type_version": evt.type_version,
            "object_version": evt.object_version,
            "actor": evt.actor_id,
            "accepted_at": evt.timestamp.isoformat() if evt.accepted_at_recorded else None,
        }
        if details.get("relation") is not None:
            entry["relationship_type"] = details.get("relation")
            entry["relationship_target"] = details.get("target_id")
        return entry

    def _relationships(self, object_id: str) -> list[dict[str, str]]:
        return [
            {"source": r.source_id, "type": r.relation, "target": r.target_id}
            for r in self._runtime.canonical_relationships(object_id)
        ]

    def _accepted_state(self, obj) -> dict[str, Any]:
        data = obj.data or {}
        fixture_id = self._fixture_of.get(obj.type)
        if fixture_id is None:
            return {**data, "status": obj.state}
        spec = self._spec(fixture_id)
        status = ft.lifecycle_field(spec) or "status"
        if fixture_id in ft.PORTABLE_IDS:
            return {**data, status: obj.state}
        return {**{f: data.get(f) for f in ft.data_fields(spec)}, status: obj.state}

    def _observe(self, object_id: str | None) -> dict[str, Any] | None:
        if not object_id:
            return None
        stored = self._runtime.get_object(object_id, raw=True)
        if stored is None:
            return None
        metadata = stored.metadata or {}
        return {
            "channel": "observer",
            "canonical_identity": stored.id,
            "type_identity": self._fixture_of.get(stored.type, stored.type),
            "type_version": stored.type_version,
            "object_version": stored.version,
            "accepted_state": self._accepted_state(stored),
            "relationships": self._relationships(object_id),
            "canonical_history_summary": [
                self._history_entry(e) for e in self._runtime.accepted_history(object_id)
            ],
            "operational_metadata": {
                "provider_binding": metadata.get("provider_binding"),
                "detached_from": metadata.get("detached_from"),
            },
        }

    def _ctx(self, req: dict[str, Any]) -> CommandContext | None:
        ev = _expected_version(req)
        return None if ev is None else CommandContext(expected_version=ev)

    def _governed(self, req: dict[str, Any], result) -> dict[str, Any]:
        outcome, unmappable = self._classify(result)
        return {
            "outcome": outcome,
            "outcome_native": f"{result.decision}/{result.outcome_category}",
            "outcome_detail": result.reason,
            "unmappable": unmappable,
            "caller_result": {"api": "CommandResult", "result": _dump(result)},
            "semantic_view": self._observe(req.get("object_id")),
        }

    def _execute(self, req: dict[str, Any], command, ctx: CommandContext | None = None) -> dict[str, Any]:
        result = self._runtime.execute(command, actor=self._actor(req["actor"]), command_context=ctx)
        body = self._governed(req, result)
        body["native_request"] = {
            "command": type(command).__name__,
            **dataclasses.asdict(command),
            "actor": req["actor"],
            "expected_version": ctx.expected_version if ctx else None,
        }
        return body

    # --- governed operations -------------------------------------------------

    def create(self, req: dict[str, Any]) -> dict[str, Any]:
        fixture_id = req["type"]
        spec = self._spec(fixture_id)
        native_type = self._kind_of[fixture_id]
        initial = dict(req.get("initial_state") or {})
        for key, value in (spec.get("create_defaults") or {}).items():
            initial.setdefault(key, value)
        lifecycle = ft.lifecycle_field(spec)
        state = initial.get(lifecycle)
        data = {k: v for k, v in initial.items() if k != lifecycle}
        rels = [
            {"source_id": r.get("source", req["object_id"]), "relation": r["type"], "target_id": r["target"]}
            for r in req.get("relationships") or []
        ]
        return self._execute(
            req,
            CreateObject(
                req["object_id"],
                native_type,
                data,
                state=state,
                metadata=req.get("operational_metadata"),
                relationships=rels,
            ),
        )

    def read(self, req: dict[str, Any]) -> dict[str, Any]:
        return self._execute(req, ReadObject(req["object_id"]))

    def _target_type(self, req: dict[str, Any]) -> str:
        """Protocol: when mutate/transition carries no ``type``, the target is a work-item."""
        return req.get("type") or WORK_ITEM

    def mutate(self, req: dict[str, Any]) -> dict[str, Any]:
        ctx = self._ctx(req)
        fixture_id = self._target_type(req)
        spec = self._spec(fixture_id)
        changes = req.get("changes") or {}
        if fixture_id in ft.PORTABLE_IDS:
            action = next(n for n, a in spec["actions"].items() if a["kind"] == "state")
            command = Command(action=action, object_id=req["object_id"], payload={"set": dict(changes)})
        else:
            if len(changes) != 1 or next(iter(changes)) not in ft.data_fields(spec):
                raise RequestError(f"{fixture_id} accepts a single data field per update, got {sorted(changes)}")
            field, value = next(iter(changes.items()))
            command = Command(action=ft.update_action(field), object_id=req["object_id"], payload={field: value})
        return self._execute(req, command, ctx)

    def transition(self, req: dict[str, Any]) -> dict[str, Any]:
        ctx = self._ctx(req)
        fixture_id = self._target_type(req)
        spec = self._spec(fixture_id)
        to_state = req["to"]
        action = ft.target_actions(spec).get(to_state)
        if action is None:
            raise RequestError(f"{fixture_id} declares no lifecycle state {to_state!r}")
        return self._execute(req, TransitionState(req["object_id"], action), ctx)

    def add_relationship(self, req: dict[str, Any]) -> dict[str, Any]:
        rel = req.get("relationship") or {}
        return self._execute(req, AddRelationship(req["object_id"], rel.get("type", ""), rel.get("target", "")), self._ctx(req))

    def remove_relationship(self, req: dict[str, Any]) -> dict[str, Any]:
        rel = req.get("relationship") or {}
        return self._execute(req, RemoveRelationship(req["object_id"], rel.get("type", ""), rel.get("target", "")), self._ctx(req))

    def detach(self, req: dict[str, Any]) -> dict[str, Any]:
        detached = self._runtime.detach_provider(req["object_id"])
        return {
            "outcome": "ACCEPTED" if detached is not None else "UNMAPPABLE",
            "outcome_native": "detached" if detached is not None else "none",
            "unmappable": detached is None,
            "caller_result": {"api": "GovernedObject | None", "result": _dump(detached)},
            "semantic_view": self._observe(req["object_id"]),
        }

    def restore(self, req: dict[str, Any]) -> dict[str, Any]:
        objects = []
        for o in req.get("objects") or []:
            ident = o["canonical_identity"]

            def rel(r: dict[str, str]) -> ImportRelationship:
                return ImportRelationship(source=ident, type=r["type"], target=r["target"])

            history = tuple(
                ImportEntry(
                    position=e["position"],
                    effect=e["effect"],
                    type_identity=o["type_identity"],
                    type_version=o["type_version"],
                    resulting_object_version=e["resulting_object_version"],
                    post_image=e.get("post_image"),
                    baseline_relationships=(
                        tuple(rel(r) for r in e["baseline_relationships"])
                        if e.get("baseline_relationships") is not None
                        else None
                    ),
                    relationship=rel(e["relationship"]) if e.get("relationship") else None,
                    evidence=ImportEvidence(actor=e.get("actor"), accepted_at=e.get("accepted_at")),
                )
                for e in o["history"]
            )
            objects.append(
                ImportObject(
                    canonical_identity=ident,
                    type_identity=o["type_identity"],
                    type_version=o["type_version"],
                    governance_reference=GOVERNANCE_REF,
                    accepted_state=dict(o["accepted_state"]),
                    relationships=tuple(rel(r) for r in o.get("relationships") or []),
                    current_object_version=o["current_object_version"],
                    history=history,
                )
            )
        bundle = ImportBundle(
            objects=tuple(objects),
            closed=False,
            spec_revision=SPEC_REVISION,
            history_completeness=HISTORY_COMPLETENESS,
            history_position_model=HISTORY_POSITION_MODEL,
            governance_reference_model=GOVERNANCE_REFERENCE_MODEL,
            source_implementation="conformance-harness",
        )
        single = objects[0].canonical_identity if len(objects) == 1 else None
        try:
            result = self._runtime.import_interchange_bundle(
                bundle,
                actor=self._actor(req["actor"]),
                provenance=ImportProvenance(source_implementation="conformance-harness", adapter="conformance-adapter-draft22"),
            )
        except ImportRefused as exc:
            refusal = {"code": exc.code, "message": exc.message, "identity": exc.identity, "position": exc.position}
            return {
                "outcome": "AUTHORIZATION_DENIED" if exc.code == IMPORT_AUTHORIZATION_DENIED else "REFUSED",
                "outcome_native": exc.code,
                "outcome_detail": str(exc),
                "unmappable": False,
                "caller_result": {"api": "ImportResult | ImportRefused", "result": None, "refusal": refusal},
                "semantic_view": self._observe(single),
            }
        return {
            "outcome": "ACCEPTED",
            "outcome_native": "imported",
            "unmappable": False,
            "caller_result": {"api": "ImportResult | ImportRefused", "result": _dump(result), "refusal": None},
            "semantic_view": self._observe(single),
        }

    # --- administrative observation -----------------------------------------

    def get_history(self, req: dict[str, Any]) -> dict[str, Any]:
        object_id = req["object_id"]
        canonical = [self._history_entry(e) for e in self._runtime.accepted_history(object_id)]
        audit = [
            {"effect": e.action, "decision": str(e.decision), "object_version": e.object_version}
            for e in self._runtime.history(object_id)
            if e.history_position is None
        ]
        return {
            "outcome": "ACCEPTED",
            "unmappable": False,
            "canonical_history": canonical,
            "audit_history": audit,
            "semantic_view": self._observe(object_id),
        }

    def replay(self, req: dict[str, Any]) -> dict[str, Any]:
        object_id = req["object_id"]
        try:
            obj = self._runtime.replay(object_id)
        except Exception as exc:  # noqa: BLE001 - runtime refusal surfaces as REFUSED
            return {"outcome": "REFUSED", "unmappable": False, "outcome_detail": f"{type(exc).__name__}: {exc}"}
        if obj is None:
            return {"outcome": "SEMANTICALLY_INVALID", "unmappable": False, "outcome_detail": "replay returned none"}
        return {
            "outcome": "ACCEPTED",
            "unmappable": False,
            "semantic_view": {
                "channel": "replay",
                "canonical_identity": obj.id,
                "type_identity": self._fixture_of.get(obj.type, obj.type),
                "type_version": obj.type_version,
                "object_version": obj.version,
                "accepted_state": self._accepted_state(obj),
                "relationships": self._relationships(object_id),
                "canonical_history_summary": [
                    self._history_entry(e) for e in self._runtime.accepted_history(object_id)
                ],
            },
        }

    def semantic_view(self, req: dict[str, Any]) -> dict[str, Any]:
        view = self._observe(req["object_id"])
        if view is None:
            return {"outcome": "SEMANTICALLY_INVALID", "unmappable": False, "semantic_view": None}
        return {"outcome": "ACCEPTED", "unmappable": False, "semantic_view": view}

    def handle(self, req: dict[str, Any]) -> dict[str, Any]:
        handlers = {
            "capabilities": lambda r: {"capabilities": self.capabilities(), "outcome": "ACCEPTED", "unmappable": False},
            "reset": lambda r: (self.reset(), {"outcome": "ACCEPTED", "unmappable": False})[1],
            "define_fixture_types": self.define_fixture_types,
            "restart": self.restart,
            "create": self.create,
            "read": self.read,
            "mutate": self.mutate,
            "transition": self.transition,
            "add_relationship": self.add_relationship,
            "remove_relationship": self.remove_relationship,
            "detach": self.detach,
            "restore": self.restore,
            "get_history": self.get_history,
            "replay": self.replay,
            "semantic_view": self.semantic_view,
            "set_clock": lambda r: {
                "outcome": "UNMAPPABLE",
                "unmappable": True,
                "outcome_detail": "no clock hook governs accepted history timestamps",
            },
        }
        op = req.get("op")
        if op not in handlers:
            return {"ok": False, "error": f"unknown op {op}", "outcome": "UNMAPPABLE", "unmappable": True}
        body = handlers[op](req)
        body["ok"] = True
        body.setdefault("error", None)
        return body


def main() -> None:
    session = AdapterSession()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        req = json.loads(line)
        resp = {"id": req.get("id")}
        try:
            resp.update(session.handle(req))
        except RequestError as exc:
            resp.update({"ok": False, "error": str(exc), "outcome": "UNMAPPABLE", "unmappable": True})
        except Exception as exc:  # noqa: BLE001 - adapter boundary
            resp.update({"ok": False, "error": f"{type(exc).__name__}: {exc}", "outcome": "UNMAPPABLE", "unmappable": True})
        sys.stdout.write(json.dumps(resp) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
