# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Brafford Labs, LLC
"""Command execution context for correlation and idempotency."""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import uuid4


@dataclass(frozen=True)
class CommandContext:
    command_id: str = field(default_factory=lambda: str(uuid4()))
    correlation_id: str = field(default_factory=lambda: str(uuid4()))
    idempotency_key: str | None = None
    application_id: str | None = None
    expected_version: int | None = None
    mandate_id: str | None = None
    purpose: str | None = None

    def to_metadata(self) -> dict:
        meta = {
            "command_id": self.command_id,
            "correlation_id": self.correlation_id,
            "idempotency_key": self.idempotency_key,
        }
        if self.application_id is not None:
            meta["application_id"] = self.application_id
        if self.expected_version is not None:
            meta["expected_version"] = self.expected_version
        if self.mandate_id is not None:
            meta["mandate_id"] = self.mandate_id
        if self.purpose is not None:
            meta["purpose"] = self.purpose
        return meta
