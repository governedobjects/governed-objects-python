# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Brafford Labs, LLC
"""Test-owned accepted-event reducer for synthetic history fixtures only."""
from governed_objects.domain.models import GovernedObject


class SyntheticReducer:
    def reduce(self, obj, event):
        if event.action == "create":
            return GovernedObject(
                id=event.object_id, type=event.object_type,
                type_version=event.type_version, version=event.object_version,
                state=event.new_state, data=dict(event.details.get("data", {})),
            )
        if obj is None:
            return None
        return obj.model_copy(update={
            "state": event.new_state or obj.state,
            "data": {**obj.data, **event.details.get("data", {})},
            "version": event.object_version,
        })
