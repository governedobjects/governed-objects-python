# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Brafford Labs, LLC
"""Application identity registry with capability boundaries."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ApplicationDefinition:
    id: str
    name: str
    capabilities: frozenset[str] = field(default_factory=frozenset)


class ApplicationRegistry:
    """Small registry of application identities for provenance and authority bounds."""

    def __init__(self) -> None:
        self._apps: dict[str, ApplicationDefinition] = {}

    def register(self, app: ApplicationDefinition) -> None:
        self._apps[app.id] = app

    def get(self, app_id: str) -> ApplicationDefinition | None:
        return self._apps.get(app_id)

    def require(self, app_id: str) -> ApplicationDefinition:
        app = self.get(app_id)
        if app is None:
            raise KeyError(f"unknown application: {app_id}")
        return app

    def unregister(self, app_id: str) -> None:
        self._apps.pop(app_id, None)

    def list_ids(self) -> list[str]:
        return sorted(self._apps)
