# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Brafford Labs, LLC
"""Draft 2.2 Portable Restoration Continuity claim of this runtime (EXPERIMENTAL).

A semantic declaration of what the native export
(:mod:`governed_objects.interchange.export`) and import
(:mod:`governed_objects.interchange.importer`) paths do, for PRC-CLAIM-001,
PRC-CAP-001, PRC-EVID-001, and PRC-HANDOFF-001. It is not an encoding or a
discovery protocol. Type bindings are read from the runtime's type registry;
every other item names the constant or behavior that the native paths enforce.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from governed_objects.interchange import EXPORT_SPEC_REVISION, IMPORT_SPEC_REVISIONS
from governed_objects.interchange.importer import NATIVE_TOKEN_GRAMMAR, REQUIRED_PROVENANCE
from governed_objects.interchange.types import portable_type_of

if TYPE_CHECKING:
    from governed_objects.types.registry import TypeRegistry

PRC_PROFILE = "Portable Restoration Continuity"
PRC_ROLES = ("exporter", "restorer")
EFFECT_EQUIVALENT = "effect-equivalent"
TIME_PRECISION = "microsecond"
# Carried provenance the importer does not retain (PRC-EVID-001); export never
# presents a value for it.
PROVENANCE_NOT_RETAINED = (
    "source_event_id",
    "action, unless it names the native action that encodes the entry's effect",
    "relationship target_type and metadata",
)
HANDOFF = {
    "model": "operator-procedure",
    "declaration": (
        "For each restored identity, the operator ensures that the source accepts no canonical "
        "change for the identity after the revision carried in the portable representation, for "
        "example by retiring the source or making it read-only for the identity, so that the "
        "source's and this runtime's accepted revisions form one lineage."
    ),
    "enforced_by_runtime": False,
    "verified_by_runtime": False,
}


def supported_type_bindings(types: TypeRegistry) -> list[str]:
    """``identity@version`` of every portable type configured in ``types``:
    the bindings the importer's Type Identity and Type Version checks accept."""
    bindings = []
    for kind in types.list_types():
        type_def = types.get(kind)
        if portable_type_of(type_def) is not None:
            bindings.append(f"{type_def.kind}@{type_def.type_version}")
    return sorted(bindings)


def prc_declaration(types: TypeRegistry) -> dict[str, Any]:
    return {
        "profile": PRC_PROFILE,
        "roles": list(PRC_ROLES),
        "export_spec_revision": EXPORT_SPEC_REVISION,
        "capabilities": {
            "spec_revisions": list(IMPORT_SPEC_REVISIONS),
            "type_bindings": supported_type_bindings(types),
            "object_version_forms": NATIVE_TOKEN_GRAMMAR,
            "baseline_relationships": True,
            "time_precision": TIME_PRECISION,
            "required_provenance": list(REQUIRED_PROVENANCE),
            "evidence_level": EFFECT_EQUIVALENT,
            "provenance_not_retained": list(PROVENANCE_NOT_RETAINED),
            "all_or_nothing": True,
        },
        "handoff": dict(HANDOFF),
    }
