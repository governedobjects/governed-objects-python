# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Brafford Labs, LLC
"""DelegatedMandate event reducer."""

from __future__ import annotations

from governed_objects.domain.models import GovernedObject, GovernanceEvent


class MandateEventReducer:
    def reduce(
        self,
        obj: GovernedObject | None,
        event: GovernanceEvent,
    ) -> GovernedObject | None:
        if event.action == "create":
            return GovernedObject(
                id=event.object_id,
                type="DelegatedMandate",
                type_version=event.type_version or "1",
                version=event.object_version,
                state=event.new_state or "draft",
                data=event.details.get("data", {}),
            )
        if obj is None:
            return None
        if event.action in {"activate", "revoke"}:
            return obj.model_copy(
                update={"state": event.new_state or obj.state, "version": event.object_version}
            )
        return obj
