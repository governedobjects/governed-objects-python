# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Brafford Labs, LLC
"""Map normalized reconciliation differences to external observations."""

from __future__ import annotations

from typing import Any

from governed_objects.observations.models import ExternalObservation, FieldChange, ObservationSource


def reconciliation_to_observation(
    *,
    object_id: str,
    provider_name: str,
    external_id: str,
    canonical: dict[str, Any],
    canonical_state: str,
    provider_data: dict[str, Any],
    provider_state: str,
    provider_type: str = "generic",
) -> ExternalObservation | None:
    """Build observation from canonical vs provider comparison."""
    changes: dict[str, FieldChange] = {}
    all_keys = set(canonical.keys()) | set(provider_data.keys())
    for field in all_keys:
        old_val = canonical.get(field)
        new_val = provider_data.get(field)
        if old_val != new_val and new_val is not None:
            changes[field] = FieldChange(previous_observed=old_val, observed=new_val)

    state_change = None
    if canonical_state != provider_state:
        state_change = FieldChange(previous_observed=canonical_state, observed=provider_state)

    if not changes and not state_change:
        return None

    return ExternalObservation(
        object_id=object_id,
        provider_type=provider_type,
        provider_name=provider_name,
        external_id=external_id,
        source=ObservationSource.RECONCILIATION,
        changes=changes,
        state_change=state_change,
        provider_version=provider_data.get("_provider_version"),
    )
