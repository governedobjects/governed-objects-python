# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Brafford Labs, LLC
"""Authorization context passed through query and command pipelines."""

from __future__ import annotations

from dataclasses import dataclass

from governed_objects.applications.registry import ApplicationDefinition
from governed_objects.delegation.mandate import DelegatedMandate
from governed_objects.delegation.resolver import ResolvedMandateAuthority


class AuthorizationContextError(ValueError):
    """Requested authorization context could not be established safely."""

    def __init__(self) -> None:
        super().__init__("authorization context unavailable")


@dataclass(frozen=True)
class AuthorizationContext:
    application: ApplicationDefinition | None = None
    mandate: DelegatedMandate | None = None
    resolved_mandate: ResolvedMandateAuthority | None = None
    purpose: str | None = None
    application_id: str | None = None
    mandate_id: str | None = None
