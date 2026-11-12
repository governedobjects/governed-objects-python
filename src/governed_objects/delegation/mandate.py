# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Brafford Labs, LLC
"""Delegated mandate model."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from governed_objects.delegation.scope import MandateScope


class MandateStatus(StrEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    REVOKED = "revoked"
    EXPIRED = "expired"


@dataclass
class DelegatedMandate:
    id: str
    principal_id: str
    delegate_actor_id: str
    capabilities: frozenset[str]
    scope: MandateScope = field(default_factory=MandateScope)
    purposes: frozenset[str] = frozenset()
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    status: MandateStatus = MandateStatus.DRAFT
    parent_mandate_id: str | None = None
    version: int = 1

    def is_active_at(self, when: datetime) -> bool:
        if self.status == MandateStatus.REVOKED:
            return False
        if self.status == MandateStatus.DRAFT:
            return False
        if self.valid_from is not None and when < self.valid_from:
            return False
        if self.valid_until is not None and when > self.valid_until:
            return False
        return self.status == MandateStatus.ACTIVE
