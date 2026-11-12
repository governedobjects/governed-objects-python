# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Brafford Labs, LLC
"""Canonical relationship names for governed object graphs."""

from __future__ import annotations

DELEGATED_BY = "delegated_by"
DELEGATED_TO = "delegated_to"
SCOPED_TO = "scoped_to"
DERIVED_FROM = "derived_from"

CANONICAL_MANDATE_RELATIONS = frozenset({DELEGATED_BY, DELEGATED_TO, SCOPED_TO, DERIVED_FROM})
