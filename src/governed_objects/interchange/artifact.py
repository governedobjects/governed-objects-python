# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Brafford Labs, LLC
"""Decode ``go-core-interchange-experiment/1`` or ``/2`` into an ``ImportBundle``.

EXPERIMENTAL. NON-NORMATIVE. NOT A WIRE STANDARD. This is the encoding layer
used by the experimental adapter; the runtime import API never sees JSON.

The artifact revision is read first and selects the contract: its status
marker and the one specification revision it carries. ``/1`` with Draft 2.2
content, ``/2`` with Draft 2.1 content, or either with any other revision is
refused here, because ``ImportBundle`` does not carry the artifact revision.

The decoder checks shape only and refuses, never repairs: duplicate members,
non-finite numbers, unknown members, non-string tokens, and a manifest that
does not describe the contents are refused. Named out-of-scope content
(policy, grants, credentials, provider bindings, ...) is refused as
UNSUPPORTED_SEMANTIC_FEATURE. Receiver semantics are the importer's job.
"""

from __future__ import annotations

import json
from typing import Any

from governed_objects.interchange import (
    ARTIFACT_CONTRACTS,
    ARTIFACT_REVISION_V2,
    IMPORT_ARTIFACT_REVISIONS,
)
from governed_objects.interchange.importer import (
    MALFORMED_ARTIFACT,
    STATE_NOT_VALID_FOR_TYPE,
    UNSUPPORTED_SEMANTIC_FEATURE,
    UNSUPPORTED_SPEC_REVISION,
    ImportBundle,
    ImportEntry,
    ImportEvidence,
    ImportObject,
    ImportRefused,
    ImportRelationship,
)

UNSUPPORTED_ARTIFACT_REVISION = "UNSUPPORTED_ARTIFACT_REVISION"

# History starts a revision recognizes but cannot carry (possibly complete under
# PRC, so not HISTORY_INCOMPLETE).
_UNCARRIED_COMPLETENESS = {ARTIFACT_REVISION_V2: ("complete-from-replay-baseline",)}

OUT_OF_SCOPE_MEMBERS = frozenset(
    {
        "policy", "policy_content", "governance_policy", "grants", "capabilities", "permissions",
        "actors", "credentials", "secrets", "trust_roots", "provider_binding", "provider_bindings",
        "observations", "mandates", "application_registrations", "profile_data",
    }
)

_MANIFEST_REQUIRED = (
    "artifact_status", "history_completeness", "history_position_model",
    "governance_reference_model", "profiles", "identities", "type_bindings",
    "governance_references", "closed",
)
_MANIFEST_OPTIONAL = ("artifact_revision", "spec_revision", "provenance")
_OBJECT_REQUIRED = (
    "canonical_identity", "type_identity", "type_version", "accepted_state",
    "relationships", "current_object_version", "history",
)
_ENTRY_REQUIRED = ("position", "effect", "type_identity", "type_version", "resulting_object_version")
_ENTRY_CONTENT = ("post_image", "relationships", "relationship")
_ENTRY_EVIDENCE = ("actor", "accepted_at", "action", "source_event_id")
_EFFECT_CONTENT = {
    "create": {"post_image", "relationships"},
    "state_change": {"post_image"},
    "lifecycle_transition": {"post_image"},
    "relationship_add": {"relationship"},
    "relationship_remove": {"relationship"},
}


def _refuse(detail: str, identity: str | None = None, position: int | None = None, code=MALFORMED_ARTIFACT):
    return ImportRefused(code, detail, identity, position)


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise _refuse(f"duplicate JSON member {key!r}")
        out[key] = value
    return out


def _constant(name: str) -> Any:
    raise _refuse(f"non-finite JSON number {name}")


def _extras(raw: dict, known: tuple[str, ...], where: str, identity=None, position=None) -> None:
    for name in raw:
        if name in known:
            continue
        if name in OUT_OF_SCOPE_MEMBERS:
            raise _refuse(f"{where}: out-of-scope content {name!r}", identity, position, UNSUPPORTED_SEMANTIC_FEATURE)
        raise _refuse(f"{where}: unrecognized member {name!r}", identity, position)


def _string(value: Any, where: str, identity=None, position=None) -> str:
    if not isinstance(value, str) or value == "":
        raise _refuse(f"{where} must be a non-empty string", identity, position)
    return value


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _relationship(raw: Any, where: str, identity, position=None) -> ImportRelationship:
    if not isinstance(raw, dict) or not {"source", "type", "target"} <= set(raw) <= {
        "source", "type", "target", "target_type", "metadata",
    }:
        raise _refuse(f"{where} must be {{source, type, target}} with optional target_type/metadata", identity, position)
    if "target_type" in raw and not isinstance(raw["target_type"], str):
        raise _refuse(f"{where}.target_type must be a string", identity, position)
    if "metadata" in raw and not isinstance(raw["metadata"], dict):
        raise _refuse(f"{where}.metadata must be an object", identity, position)
    return ImportRelationship(
        _string(raw["source"], f"{where}.source", identity, position),
        _string(raw["type"], f"{where}.type", identity, position),
        _string(raw["target"], f"{where}.target", identity, position),
        has_evidence="target_type" in raw or "metadata" in raw,
    )


def _relationships(raw: Any, where: str, identity, position=None) -> tuple[ImportRelationship, ...]:
    if not isinstance(raw, list):
        raise _refuse(f"{where} must be a list", identity, position)
    return tuple(_relationship(r, f"{where}[{i}]", identity, position) for i, r in enumerate(raw))


def _state(raw: Any, where: str, identity, position=None) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise _refuse(f"{where} must be an object", identity, position)
    for name, value in raw.items():
        if not isinstance(value, str):
            raise ImportRefused(
                STATE_NOT_VALID_FOR_TYPE, f"{where}.{name} must be a string (never coerced)", identity, position
            )
    return dict(raw)


def _token(value: Any, where: str, identity, position=None) -> str:
    if not isinstance(value, str) or value == "":
        raise _refuse(f"{where} {value!r} is not an opaque string token (never coerced)", identity, position)
    return value


def _entry(raw: Any, identity: str, index: int) -> ImportEntry:
    where = f"history[{index}]"
    if not isinstance(raw, dict):
        raise _refuse(f"{where} must be an object", identity)
    for name in _ENTRY_REQUIRED:
        if name not in raw:
            raise _refuse(f"{where}: required member {name!r} missing", identity)
    position = raw["position"]
    if not _is_int(position):
        raise _refuse(f"{where}.position must be a JSON integer", identity)
    effect = raw["effect"]
    if not isinstance(effect, str) or effect not in _EFFECT_CONTENT:
        raise _refuse(f"{where}.effect {effect!r} is not a known effect kind", identity, position)
    present = {n for n in _ENTRY_CONTENT if n in raw}
    if present != _EFFECT_CONTENT[effect]:
        raise _refuse(f"{where}: effect {effect!r} requires exactly {sorted(_EFFECT_CONTENT[effect])}", identity, position)
    _extras(raw, _ENTRY_REQUIRED + _ENTRY_CONTENT + _ENTRY_EVIDENCE, where, identity, position)
    for name in _ENTRY_EVIDENCE:
        if name in raw and not isinstance(raw[name], str):
            raise _refuse(f"{where}.{name} must be a string when present", identity, position)
    return ImportEntry(
        position=position,
        effect=effect,
        type_identity=_string(raw["type_identity"], f"{where}.type_identity", identity, position),
        type_version=_string(raw["type_version"], f"{where}.type_version", identity, position),
        resulting_object_version=_token(raw["resulting_object_version"], f"{where}.resulting_object_version", identity, position),
        post_image=_state(raw["post_image"], f"{where}.post_image", identity, position) if "post_image" in raw else None,
        baseline_relationships=(
            _relationships(raw["relationships"], f"{where}.relationships", identity, position)
            if "relationships" in raw else None
        ),
        relationship=(
            _relationship(raw["relationship"], f"{where}.relationship", identity, position)
            if "relationship" in raw else None
        ),
        evidence=ImportEvidence(
            actor=raw.get("actor"),
            accepted_at=raw.get("accepted_at"),
            action=raw.get("action"),
            source_event_id=raw.get("source_event_id"),
        ),
    )


def _object(raw: Any, index: int) -> ImportObject:
    where = f"objects[{index}]"
    if not isinstance(raw, dict):
        raise _refuse(f"{where} must be an object")
    for name in _OBJECT_REQUIRED:
        if name not in raw:
            raise _refuse(f"{where}: required member {name!r} missing")
    identity = _string(raw["canonical_identity"], f"{where}.canonical_identity")
    _extras(raw, _OBJECT_REQUIRED + ("governance_reference",), "object", identity)
    governance = raw.get("governance_reference")
    if governance is not None and not isinstance(governance, str):
        raise _refuse("governance_reference must be an opaque string", identity)
    if not isinstance(raw["history"], list):
        raise _refuse("history must be a list", identity)
    return ImportObject(
        canonical_identity=identity,
        type_identity=_string(raw["type_identity"], "type_identity", identity),
        type_version=_string(raw["type_version"], "type_version", identity),
        governance_reference=governance,
        accepted_state=_state(raw["accepted_state"], "accepted_state", identity),
        relationships=_relationships(raw["relationships"], "relationships", identity),
        current_object_version=_token(raw["current_object_version"], "current_object_version", identity),
        history=tuple(_entry(e, identity, i) for i, e in enumerate(raw["history"])),
    )


def decode_artifact(text: str) -> ImportBundle:
    try:
        doc = json.loads(text, object_pairs_hook=_pairs, parse_constant=_constant)
    except ImportRefused:
        raise
    except (ValueError, TypeError) as exc:
        raise _refuse(f"unparseable JSON: {exc}") from None
    manifest = doc.get("manifest") if isinstance(doc, dict) else None
    revision = manifest.get("artifact_revision") if isinstance(manifest, dict) else None
    contract = ARTIFACT_CONTRACTS.get(revision) if isinstance(revision, str) else None
    if contract is None:
        raise ImportRefused(
            UNSUPPORTED_ARTIFACT_REVISION,
            f"artifact revision {revision!r} is not one of {list(IMPORT_ARTIFACT_REVISIONS)}",
        )
    status, carried_spec = contract
    if "objects" not in doc:
        raise _refuse("bundle: required member 'objects' missing")
    _extras(doc, ("manifest", "objects"), "bundle")
    for name in _MANIFEST_REQUIRED:
        if name not in manifest:
            raise _refuse(f"manifest: required member {name!r} missing")
    _extras(manifest, _MANIFEST_REQUIRED + _MANIFEST_OPTIONAL, "manifest")
    if manifest["artifact_status"] != status:
        raise _refuse(f"manifest.artifact_status does not carry the {revision} status marker")
    spec = manifest.get("spec_revision")
    if spec is not None and not isinstance(spec, str):
        raise _refuse("manifest.spec_revision must be a string")
    for name in ("history_completeness", "history_position_model", "governance_reference_model"):
        _string(manifest[name], f"manifest.{name}")
    for name in ("profiles", "identities", "type_bindings", "governance_references"):
        if not isinstance(manifest[name], list):
            raise _refuse(f"manifest.{name} must be a list")
    if not isinstance(manifest["closed"], bool):
        raise _refuse("manifest.closed must be a boolean")
    source_implementation = None
    if "provenance" in manifest:
        p = manifest["provenance"]
        if not isinstance(p, dict) or not set(p) <= {"source_implementation", "exported_at"} or not all(
            isinstance(v, str) for v in p.values()
        ):
            raise _refuse("manifest.provenance may contain only source_implementation and exported_at strings")
        source_implementation = p.get("source_implementation")
    if not isinstance(doc["objects"], list):
        raise _refuse("objects must be a list")
    objects = tuple(_object(o, i) for i, o in enumerate(doc["objects"]))
    if spec != carried_spec:
        raise ImportRefused(
            UNSUPPORTED_SPEC_REVISION,
            f"artifact revision {revision} carries only {carried_spec!r} content, not {spec!r}",
        )
    if manifest["history_completeness"] in _UNCARRIED_COMPLETENESS.get(revision, ()):
        raise _refuse(
            f"history completeness {manifest['history_completeness']!r} cannot be carried by {revision}",
            code=UNSUPPORTED_SEMANTIC_FEATURE,
        )

    bindings = []
    for i, b in enumerate(manifest["type_bindings"]):
        if not isinstance(b, dict) or set(b) != {"type_identity", "type_version"}:
            raise _refuse(f"manifest.type_bindings[{i}] must be {{type_identity, type_version}}")
        bindings.append((_string(b["type_identity"], "type_identity"), _string(b["type_version"], "type_version")))
    declared = {
        "identities": [_string(x, "manifest.identities[]") for x in manifest["identities"]],
        "type_bindings": bindings,
        "governance_references": [_string(x, "manifest.governance_references[]") for x in manifest["governance_references"]],
    }
    actual = {
        "identities": {o.canonical_identity for o in objects},
        "type_bindings": {(o.type_identity, o.type_version) for o in objects},
        "governance_references": {o.governance_reference for o in objects if o.governance_reference},
    }
    for name, values in declared.items():
        if len(set(values)) != len(values) or set(values) != actual[name]:
            raise _refuse(f"manifest.{name} does not describe the bundle contents")

    return ImportBundle(
        objects=objects,
        closed=manifest["closed"],
        spec_revision=spec,
        history_completeness=manifest["history_completeness"],
        history_position_model=manifest["history_position_model"],
        governance_reference_model=manifest["governance_reference_model"],
        profiles=tuple(manifest["profiles"]),
        source_implementation=source_implementation,
    )
