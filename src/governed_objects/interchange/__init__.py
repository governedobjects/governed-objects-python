# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Brafford Labs, LLC
"""Provisional Core interchange (EXPERIMENTAL).

EXPERIMENTAL. NON-NORMATIVE. NOT A GOVERNED OBJECT WIRE STANDARD.
NOT A DRAFT 2.1 OR DRAFT 2.2 REQUIREMENT.

Export and new-target import of the frozen provisional
artifacts of the interchange experiment: ``go-core-interchange-experiment/1``
(Draft 2.1 content only) and its successor ``go-core-interchange-experiment/2``
(Draft 2.2 content only). Export
writes the successor; import restores either. Import restores accepted history
into an empty target; it is not sync, merge, or replication.

The artifact constants below are serialization constants of those artifact
revisions. Each artifact revision carries exactly one specification revision,
so the pair is part of artifact validity; the runtime's supported
specification revisions are a capability, not an artifact member.
"""

ARTIFACT_REVISION_V1 = "go-core-interchange-experiment/1"
ARTIFACT_STATUS_V1 = (
    "EXPERIMENTAL; NON-NORMATIVE; NOT A GOVERNED OBJECT WIRE STANDARD; "
    "NOT A DRAFT 2.1 REQUIREMENT"
)
ARTIFACT_REVISION_V2 = "go-core-interchange-experiment/2"
ARTIFACT_STATUS_V2 = (
    "EXPERIMENTAL; NON-NORMATIVE; NOT A GOVERNED OBJECT WIRE STANDARD; "
    "NOT A DRAFT 2.2 REQUIREMENT"
)
EXPORT_SPEC_REVISION = "v0alpha1/draft2.2"
# Public name for the Draft 2.1 revision the importer restores (artifact v1); it
# stays a single revision string, not the set of input revisions.
SPEC_REVISION = "v0alpha1/draft2.1"
# artifact revision -> (status marker, the one specification revision it carries)
ARTIFACT_CONTRACTS = {
    ARTIFACT_REVISION_V1: (ARTIFACT_STATUS_V1, SPEC_REVISION),
    ARTIFACT_REVISION_V2: (ARTIFACT_STATUS_V2, EXPORT_SPEC_REVISION),
}
EXPORT_ARTIFACT_REVISION = ARTIFACT_REVISION_V2
IMPORT_ARTIFACT_REVISIONS = tuple(ARTIFACT_CONTRACTS)
IMPORT_SPEC_REVISIONS = tuple(spec for _, spec in ARTIFACT_CONTRACTS.values())
HISTORY_COMPLETENESS = "complete-from-creation"
HISTORY_POSITION_MODEL = "explicit-per-object-contiguous-from-1"
GOVERNANCE_REFERENCE_MODEL = "type-scoped"

EXPORT_ORDER_INDETERMINATE = "EXPORT_ORDER_INDETERMINATE"
EXPORT_HISTORY_UNCLASSIFIABLE = "EXPORT_HISTORY_UNCLASSIFIABLE"
UNSUPPORTED_SEMANTIC_FEATURE = "UNSUPPORTED_SEMANTIC_FEATURE"
