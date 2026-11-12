# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Brafford Labs, LLC
"""Versioned actor manifest model, used to generate the published actor JSON Schema.

This is a description of the manifest format only. ``ActorRegistry`` remains the
loader and does not depend on this module.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from governed_objects.domain.models import ActorType


class ActorMetadata(BaseModel):
    """Identity metadata for an actor manifest."""

    id: str
    name: str | None = None


class ActorSpecBody(BaseModel):
    """Declarative actor capabilities."""

    type: ActorType
    capabilities: list[str] = Field(default_factory=list)


class ActorResource(BaseModel):
    """Versioned actor manifest."""

    api_version: str = Field(alias="apiVersion")
    kind: Literal["Actor"]
    metadata: ActorMetadata
    spec: ActorSpecBody

    model_config = {"populate_by_name": True}
