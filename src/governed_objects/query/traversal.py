# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Brafford Labs, LLC
"""Permission-aware graph traversal."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable

from governed_objects.adapters.base import TypeAdapter
from governed_objects.adapters.context import AdapterContext
from governed_objects.domain.models import Actor, GovernedObject
from governed_objects.domain.traversal import TraversalResult, TraversalStep
from governed_objects.materialization.service import MaterializationService
from governed_objects.persistence.sqlite import SQLiteStore
from governed_objects.policy.context import AuthorizationContext, AuthorizationContextError
from governed_objects.policy.evaluator import CapabilityPolicyEvaluator, PolicyEvaluator
from governed_objects.types.registry import TypeRegistry


class GraphTraversalService:
    """Traverse Governed Object relationships with authorization at each hop."""

    def __init__(
        self,
        store: SQLiteStore,
        type_registry: TypeRegistry,
        policy: PolicyEvaluator | None = None,
        adapters: list[TypeAdapter] | None = None,
        materializer: MaterializationService | None = None,
        context: AdapterContext | None = None,
        *,
        resolve_auth: Callable[[AuthorizationContext | None], AuthorizationContext | None] | None = None,
    ) -> None:
        self._store = store
        self._types = type_registry
        self._policy = policy or CapabilityPolicyEvaluator()
        self._adapters = [] if adapters is None else adapters
        self._materializer = materializer
        self._context = context
        self._resolve_auth = resolve_auth

    def traverse(
        self,
        actor: Actor,
        start_object_id: str,
        *,
        max_depth: int = 3,
        relations: list[str] | None = None,
        auth: AuthorizationContext | None = None,
    ) -> list[TraversalResult]:
        if self._resolve_auth is not None:
            auth = self._resolve_auth(auth)
        elif auth is not None:
            raise AuthorizationContextError()
        start = self._materialize(start_object_id, actor, auth=auth)
        if start is None or not self._can_read(actor, start, auth=auth):
            return []

        results: list[TraversalResult] = []
        visited: set[str] = set()
        queue: deque[tuple[str, list[TraversalStep], int]] = deque()
        queue.append((start_object_id, [], 0))

        while queue:
            node_id, path, depth = queue.popleft()
            if node_id in visited:
                continue

            obj = self._materialize(node_id, actor, auth=auth)
            if obj is None or not self._can_read(actor, obj, auth=auth):
                continue

            visited.add(node_id)
            results.append(
                TraversalResult(
                    object=obj,
                    depth=depth,
                    path=path,
                )
            )

            if depth >= max_depth:
                continue

            for rel, neighbor_id, step_from, step_to in self._neighbors(node_id):
                if relations is not None and rel.relation not in relations:
                    continue
                if neighbor_id in visited:
                    continue

                neighbor = self._materialize(neighbor_id, actor, auth=auth)
                if neighbor is None or not self._can_read(actor, neighbor, auth=auth):
                    continue

                step = TraversalStep(relation=rel.relation, from_id=step_from, to_id=step_to)
                queue.append((neighbor_id, path + [step], depth + 1))

        return results

    def _materialize(
        self, object_id: str, actor: Actor, *, auth: AuthorizationContext | None = None,
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

    def _neighbors(self, object_id: str):
        for rel in self._store.list_for_source(object_id):
            yield rel, rel.target_id, rel.source_id, rel.target_id
        for rel in self._store.list_for_target(object_id):
            yield rel, rel.source_id, rel.target_id, rel.source_id

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
