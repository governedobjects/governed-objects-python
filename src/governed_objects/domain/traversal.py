# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Brafford Labs, LLC
"""Traversal result models."""

from __future__ import annotations

from pydantic import BaseModel, Field

from governed_objects.domain.models import GovernedObject


class TraversalStep(BaseModel):
    """One hop in a relationship path from the traversal start."""

    relation: str
    from_id: str
    to_id: str


class TraversalResult(BaseModel):
    """A governed object reached during authorized graph traversal."""

    object: GovernedObject
    depth: int
    path: list[TraversalStep] = Field(default_factory=list)
