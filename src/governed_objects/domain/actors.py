# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Brafford Labs, LLC
"""Actor loading for the governed object runtime."""

from __future__ import annotations

from pathlib import Path

import yaml

from governed_objects.domain.models import Actor, ActorType


class ActorRegistry:
    """Resolve actors from YAML manifests (governed_objects format; legacy apiVersion labels are accepted and ignored)."""

    def __init__(self, actors_dir: Path) -> None:
        self._actors_dir = actors_dir
        self._cache: dict[str, Actor] = {}

    def resolve(self, actor_id: str) -> Actor:
        if actor_id in self._cache:
            return self._cache[actor_id]

        actor_path = self._actors_dir / f"{actor_id}.yaml"
        if not actor_path.exists():
            raise ValueError(f"Unknown actor: {actor_id}")

        raw = yaml.safe_load(actor_path.read_text()) or {}
        actor = self._parse_actor(raw, actor_id)
        self._cache[actor_id] = actor
        return actor

    def list_actors(self) -> list[str]:
        return sorted(p.stem for p in self._actors_dir.glob("*.yaml"))

    @staticmethod
    def _parse_actor(raw: dict, actor_id: str) -> Actor:
        if raw.get("kind") == "Actor" and "spec" in raw:
            spec = raw["spec"]
            meta = raw.get("metadata") or {}
            return Actor(
                id=meta.get("id", actor_id),
                type=ActorType(spec.get("type", "human")),
                capabilities=spec.get("capabilities") or [],
                constraints=spec.get("constraints") or [],
                display_name=meta.get("name"),
            )
        return Actor(
            id=raw.get("id", actor_id),
            type=ActorType(raw.get("type", "human")),
            capabilities=raw.get("capabilities") or [],
            constraints=raw.get("constraints") or [],
            display_name=raw.get("name"),
        )
