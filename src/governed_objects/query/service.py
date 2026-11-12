# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Brafford Labs, LLC
"""Permission-aware query service."""

from __future__ import annotations

from collections.abc import Callable

from governed_objects.adapters.context import AdapterContext
from governed_objects.domain.models import Actor, GovernedObject
from governed_objects.materialization.service import MaterializationService
from governed_objects.persistence.sqlite import SQLiteStore
from governed_objects.policy.context import AuthorizationContext, AuthorizationContextError
from governed_objects.policy.evaluator import CapabilityPolicyEvaluator, PolicyEvaluator
from governed_objects.types.registry import TypeRegistry


class QueryService:
    """Query governed objects with authorization applied before return."""

    def __init__(
        self,
        store: SQLiteStore,
        type_registry: TypeRegistry,
        policy: PolicyEvaluator | None = None,
        materializer: MaterializationService | None = None,
        context: AdapterContext | None = None,
        *,
        resolve_auth: Callable[[AuthorizationContext | None], AuthorizationContext | None] | None = None,
    ) -> None:
        self._store = store
        self._types = type_registry
        self._policy = policy or CapabilityPolicyEvaluator()
        self._materializer = materializer
        self._context = context
        self._resolve_auth = resolve_auth

    def query(
        self,
        actor: Actor,
        *,
        object_type: str | None = None,
        object_types: list[str] | None = None,
        state: str | None = None,
        include_subtypes: bool = False,
        auth: AuthorizationContext | None = None,
    ) -> list[GovernedObject]:
        auth = self._current_auth(auth)
        if object_types:
            seen: set[str] = set()
            results: list[GovernedObject] = []
            for t in object_types:
                for obj in self.query(
                    actor,
                    object_type=t,
                    state=state,
                    include_subtypes=include_subtypes,
                    auth=auth,
                ):
                    if obj.id not in seen:
                        seen.add(obj.id)
                        results.append(obj)
            return results

        kinds: list[str] | None = None
        if object_type:
            kinds = self._types.kinds_matching(object_type, include_subtypes=include_subtypes)

        if kinds and state:
            candidates: list[GovernedObject] = []
            for kind in kinds:
                candidates.extend(self._store.list_by_state(kind, state))
        elif kinds:
            candidates = self._store.list_by_types(kinds)
        elif object_type and state:
            candidates = self._store.list_by_state(object_type, state)
        elif object_type:
            candidates = self._store.list_by_type(object_type)
        else:
            candidates = self._store.list_all()

        results: list[GovernedObject] = []
        for obj in candidates:
            materialized = self._materialize_for_actor(obj.id, actor, auth=auth)
            if materialized and self._can_read(actor, materialized, auth=auth):
                results.append(materialized)
        return results

    def related(
        self,
        actor: Actor,
        object_id: str,
        *,
        auth: AuthorizationContext | None = None,
    ) -> list[GovernedObject]:
        auth = self._current_auth(auth)
        root = self._store.get(object_id)
        if root is None or not self._can_read(actor, root, auth=auth):
            return []
        related_ids: set[str] = set()
        for rel in self._store.list_for_source(object_id):
            related_ids.add(rel.target_id)
        for rel in self._store.list_for_target(object_id):
            related_ids.add(rel.source_id)

        results: list[GovernedObject] = []
        for rid in sorted(related_ids):
            materialized = self._materialize_for_actor(rid, actor, auth=auth)
            if materialized and self._can_read(actor, materialized, auth=auth):
                results.append(materialized)
        return results

    def _materialize_for_actor(
        self,
        object_id: str,
        actor: Actor,
        *,
        auth: AuthorizationContext | None = None,
    ) -> GovernedObject | None:
        obj = self._store.get(object_id)
        if obj is None or not self._can_read(actor, obj, auth=auth):
            return None
        if self._materializer is None or self._context is None:
            return obj
        try:
            return self._materializer.materialize(
                object_id,
                self._context,
                actor,
                hydrate=True,
                field_filter=True,
                persist=True,
            )
        except Exception:
            return None

    def _current_auth(self, auth: AuthorizationContext | None) -> AuthorizationContext | None:
        if self._resolve_auth is not None:
            return self._resolve_auth(auth)
        if auth is not None:
            raise AuthorizationContextError()
        return None

    def _can_read(
        self,
        actor: Actor,
        obj: GovernedObject,
        *,
        auth: AuthorizationContext | None = None,
    ) -> bool:
        if not self._types.supports_type_version(obj.type, obj.type_version):
            return False
        try:
            type_def = self._types.get(obj.type)
        except KeyError:
            return False
        application = auth.application if auth else None
        mandate = auth.mandate if auth else None
        resolved_mandate = auth.resolved_mandate if auth else None
        purpose = auth.purpose if auth else None
        decision = self._policy.evaluate(
            actor,
            "read",
            obj,
            type_def,
            application=application,
            mandate=mandate,
            resolved_mandate=resolved_mandate,
            purpose=purpose,
        )
        return decision.decision.value == "ALLOW"
