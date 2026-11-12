# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Brafford Labs, LLC
"""Structured provider failure types."""

from __future__ import annotations


class ProviderError(Exception):
    """Base class for provider-layer failures."""

    code: str = "PROVIDER_ERROR"

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        details: dict | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        self.details = details or {}


class ProviderObjectNotFound(ProviderError):
    code = "PROVIDER_NOT_FOUND"


class ProviderUnavailable(ProviderError):
    code = "PROVIDER_UNAVAILABLE"


class ProviderConflict(ProviderError):
    code = "PROVIDER_CONFLICT"


class ProviderInvalidResponse(ProviderError):
    code = "PROVIDER_INVALID_RESPONSE"


class ProviderOutcomeUnknown(ProviderError):
    code = "PROVIDER_OUTCOME_UNKNOWN"


class ProviderProjectionUnsupported(ProviderError):
    code = "UNSUPPORTED_PROVIDER"
