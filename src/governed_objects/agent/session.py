# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Brafford Labs, LLC
"""Thin agent session wrapper over the governed object runtime."""

from __future__ import annotations

from governed_objects.domain.command import Command, ReadObject
from governed_objects.domain.command_context import CommandContext
from governed_objects.domain.models import Actor, CommandResult, Decision, GovernanceEvent, GovernedObject
from governed_objects.domain.traversal import TraversalResult
from governed_objects.policy.context import AuthorizationContext, AuthorizationContextError
from governed_objects.runtime.runtime import GovernedObjectRuntime


class GovernedAgentSession:
    """Carries agent, application, mandate, and purpose on every operation."""

    def __init__(
        self,
        runtime: GovernedObjectRuntime,
        *,
        actor: Actor,
        application_id: str,
        mandate_id: str,
        purpose: str,
    ) -> None:
        if (not isinstance(actor, Actor)
                or any(not isinstance(value, str) or not value for value in (application_id, mandate_id))
                or (purpose is not None and (not isinstance(purpose, str) or not purpose))):
            raise AuthorizationContextError()
        self._runtime = runtime
        self._actor = actor
        self._application_id = application_id
        self._mandate_id = mandate_id
        self._purpose = purpose

    @property
    def actor(self) -> Actor:
        return self._actor

    @property
    def auth(self) -> AuthorizationContext:
        return self._runtime.resolve_authorization_context(AuthorizationContext(
            application_id=self._application_id,
            mandate_id=self._mandate_id,
            purpose=self._purpose,
        ))

    def _ctx(self, *, expected_version: int | None = None) -> CommandContext:
        return CommandContext(
            application_id=self._application_id,
            mandate_id=self._mandate_id,
            purpose=self._purpose,
            expected_version=expected_version,
        )

    def query(
        self,
        *,
        object_type: str | None = None,
        state: str | None = None,
        include_subtypes: bool = False,
    ) -> list[GovernedObject]:
        return self._runtime.query(
            self._actor,
            object_type=object_type,
            state=state,
            include_subtypes=include_subtypes,
            auth=self.auth,
        )

    def read(self, object_id: str) -> CommandResult:
        return self._runtime.execute(
            ReadObject(object_id),
            actor=self._actor,
            command_context=self._ctx(),
        )

    def traverse(
        self,
        start_object_id: str,
        *,
        max_depth: int = 3,
        relations: list[str] | None = None,
    ) -> list[TraversalResult]:
        return self._runtime.traverse(
            self._actor,
            start_object_id,
            max_depth=max_depth,
            relations=relations,
            auth=self.auth,
        )

    def execute(
        self,
        command: Command,
        *,
        expected_version: int | None = None,
    ) -> CommandResult:
        return self._runtime.execute(
            command,
            actor=self._actor,
            command_context=self._ctx(expected_version=expected_version),
        )

    def history(self, object_id: str) -> list[GovernanceEvent]:
        """Return event history only if read is authorized."""
        read = self.read(object_id)
        if read.decision != Decision.ALLOW:
            return []
        return self._runtime.history(object_id)
