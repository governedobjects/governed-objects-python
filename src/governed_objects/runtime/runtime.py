# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Brafford Labs, LLC
"""Governed Object runtime — single command execution pipeline."""

from __future__ import annotations

from typing import Any

from governed_objects.applications.registry import ApplicationDefinition, ApplicationRegistry
from governed_objects.adapters.context import AdapterContext, AdapterExecuteResult, AdapterFailure
from governed_objects.adapters.registry import AdapterRegistry
from governed_objects.domain.actors import ActorRegistry
from governed_objects.domain.command import (
    AddRelationship,
    Command,
    CreateObject,
    ReadObject,
    RemoveRelationship,
    TransitionState,
)
from governed_objects.domain.models import (
    Actor,
    ActorType,
    CommandResult,
    Decision,
    GovernedObject,
    GovernanceEvent,
    Relationship,
    ValidationCheck,
)
from governed_objects.delegation.projection import MandateProjection
from governed_objects.delegation.configuration import DelegationConfiguration, DelegationConfigurationError
from governed_objects.delegation.materializer import materialize_mandate
from governed_objects.delegation.mandate import DelegatedMandate
from governed_objects.delegation.resolver import MandateResolver, ResolvedMandateAuthority
from governed_objects.delegation.service import MandateService
from governed_objects.delegation.validator import DelegationSpec, DelegationValidator
from governed_objects.domain.clock import Clock, SystemClock
from governed_objects.domain.command_context import CommandContext
from governed_objects.domain.traversal import TraversalResult
from governed_objects.events.replay import EventReplayer, ReplayRefused, bound_type_definition
from governed_objects.idempotency.store import IdempotencyStore
from governed_objects.interchange.governance import TypeScopedGovernance
from governed_objects.materialization.service import MaterializationService
from governed_objects.persistence.sqlite import INT64_MAX, SQLiteStore
from governed_objects.policy.context import AuthorizationContext, AuthorizationContextError
from governed_objects.policy.evaluator import CapabilityPolicyEvaluator, PolicyEvaluator
from governed_objects.policy.outcomes import (
    REFUSAL_DENY_REASONS,
    REFUSED,
    OutcomeCategory,
    classify_outcome,
)
from governed_objects.policy.reasons import ConflictReason, DenyReason
from governed_objects.relationships.canonical import (
    canonical_relationship,
    is_canonical,
    list_canonical_for_object,
    replace_canonical_for_object,
)
from governed_objects.providers.errors import ProviderError
from governed_objects.providers.registry import ProviderRegistry
from governed_objects.query.service import QueryService
from governed_objects.query.traversal import GraphTraversalService
from governed_objects.registration.detach import detach_from_provider
from governed_objects.registration.service import RegistrationService
from governed_objects.relationships.sync import RelationshipSyncService
from governed_objects.observations.ingestion import ObservationIngestionService
from governed_objects.observations.store import ObservationStore
from governed_objects.outbound.service import OutboundReconciliationService
from governed_objects.outbound.store import RepairStore
from governed_objects.reconciliation.service import ReconciliationService
from governed_objects.types.extensions import TypeExtensionRegistry
from governed_objects.types.lifecycle import LifecycleValidator
from governed_objects.types.registry import TypeRegistry
from governed_objects.types.validation import SchemaValidator

class GovernedObjectRuntime:
    """Shared runtime for all governed object types."""

    @classmethod
    def configured(
        cls,
        *,
        store: SQLiteStore,
        type_registry: TypeRegistry,
        adapters: list,
        extension_registry: TypeExtensionRegistry,
        providers: ProviderRegistry,
        application_registry: ApplicationRegistry,
        policy: PolicyEvaluator | None = None,
        actors: ActorRegistry | None = None,
        clock: Clock | None = None,
        governance: TypeScopedGovernance | None = None,
        delegation: DelegationConfiguration | None = None,
    ) -> GovernedObjectRuntime:
        """Construct the common runtime with explicit, possibly empty dependencies.

        No historical configuration factories are selected on this path. Optional
        profile helpers still need their own supported representations and authority.
        """
        return cls(
            store=store, type_registry=type_registry, adapters=adapters,
            extension_registry=extension_registry, providers=providers,
            application_registry=application_registry, policy=policy, actors=actors,
            clock=clock, governance=governance, delegation=delegation,
        )

    def __init__(
        self,
        store: SQLiteStore,
        *,
        type_registry: TypeRegistry,
        adapters: list,
        extension_registry: TypeExtensionRegistry,
        providers: ProviderRegistry,
        application_registry: ApplicationRegistry,
        policy: PolicyEvaluator | None = None,
        actors: ActorRegistry | None = None,
        clock: Clock | None = None,
        governance: TypeScopedGovernance | None = None,
        delegation: DelegationConfiguration | None = None,
    ) -> None:
        for name, value in (
            ("store", store), ("type_registry", type_registry), ("adapters", adapters),
            ("extension_registry", extension_registry), ("providers", providers),
            ("application_registry", application_registry),
        ):
            if value is None:
                raise ValueError(f"runtime requires non-None {name}")
        self._store = store
        self._governance = governance
        self._clock = clock or SystemClock()
        self._types = type_registry
        self._extensions = extension_registry
        self._policy = policy or CapabilityPolicyEvaluator(
            clock=self._clock, store=store
        )
        self._applications = application_registry
        self._actors = actors
        self._delegation = delegation
        self._mandate_resolver = MandateResolver(
            store, clock=self._clock, validate_binding=self._validate_delegation_binding,
            scope_decoder=self._decode_legacy_scope,
        )
        self._delegation_validator = DelegationValidator(store)
        self._mandate_projection = MandateProjection(store, resolver=self._mandate_resolver)
        self._mandate_service = MandateService(
            self,
            self._mandate_resolver,
            self._delegation_validator,
            actors,
        )
        self._lifecycle = LifecycleValidator()
        self._schema = SchemaValidator()
        self._providers = providers
        self._adapters_list = adapters
        self._adapters = AdapterRegistry(self._adapters_list)
        if delegation is not None:
            self.require_delegation()
        if isinstance(self._policy, CapabilityPolicyEvaluator):
            self._policy.bind_context_sources(
                application_source=self._applications.get,
                mandate_source=self._mandate_resolver.resolve_authority,
            )
        self._context = AdapterContext(store, self._providers, self._types)
        self._rel_sync = RelationshipSyncService(store)
        self._materializer = MaterializationService(
            store, self._adapters, self._types, self._rel_sync
        )
        self._observation_store = ObservationStore(store)
        self._repair_store = RepairStore(store)
        self._outbound = OutboundReconciliationService(
            store, self._adapters, self._providers, self._types, self._repair_store
        )
        self._observations = ObservationIngestionService(
            store,
            self._types,
            self._observation_store,
            self._rel_sync,
            self._adapters,
            self._context,
            outbound=self._outbound,
        )
        self._registration = RegistrationService(
            store, self._adapters, self._materializer, self._types, self._observations, self._rel_sync
        )
        self._replayer = EventReplayer(store, self._extensions, self._types)
        self._reconciliation = ReconciliationService(store, self._adapters, self._observations)
        self._idempotency = IdempotencyStore(store)
        self._query = QueryService(
            store, self._types, self._policy, self._materializer, self._context,
            resolve_auth=self.resolve_authorization_context,
        )
        self._traversal = GraphTraversalService(
            store, self._types, self._policy, self._adapters_list, self._materializer, self._context,
            resolve_auth=self.resolve_authorization_context,
        )

    @property
    def providers(self) -> ProviderRegistry:
        return self._providers

    @property
    def registration(self) -> RegistrationService:
        return self._registration

    @property
    def context(self) -> AdapterContext:
        return self._context

    @property
    def mandates(self) -> MandateService:
        return self._mandate_service

    @property
    def mandate_resolver(self) -> MandateResolver:
        return self._mandate_resolver

    @property
    def mandate_projection(self) -> MandateProjection:
        return self._mandate_projection

    def _make_context(self, **kwargs) -> CommandContext:
        return CommandContext(**kwargs)

    @property
    def clock(self) -> Clock:
        return self._clock

    def execute(
        self,
        command: Command,
        *,
        actor: Actor,
        command_context: CommandContext | None = None,
    ) -> CommandResult:
        result = self._dispatch(command, actor, command_context)
        return self._shape_for_requester(result, command, actor, command_context)

    # Protected-content boundary (Draft 2.2 AUTH-DISCLOSE-001). Every caller-
    # visible CommandResult passes through here. Stored audit rows are not
    # affected; only the returned result is shaped.
    _GENERIC_REJECTION_REASON = {
        OutcomeCategory.CONFLICT: "version conflict",
        OutcomeCategory.SEMANTICALLY_INVALID: "command rejected",
        REFUSED: "command refused",
    }

    def _shape_for_requester(
        self,
        result: CommandResult,
        command: Command,
        actor: Actor,
        command_context: CommandContext | None,
    ) -> CommandResult:
        result = self._finalize(result)
        if result.decision == Decision.ALLOW:
            return result
        stored = self._store.get(command.object_id) if command.object_id else None
        if stored is None or self._requester_can_read(actor, stored, command_context):
            return result
        result.object_version = None
        result.actual_version = None
        result.previous_state = None
        result.new_state = None
        result.data = {}
        result.events = []
        result.validation_checks = [
            ValidationCheck(name=c.name, passed=c.passed, message="") for c in result.validation_checks
        ] if self._types.supports_type_version(stored.type, stored.type_version) else []
        if result.outcome_category != OutcomeCategory.AUTHORIZATION_DENIED:
            result.reason = self._GENERIC_REJECTION_REASON.get(
                result.outcome_category, "command rejected"
            )
        return result

    def _requester_can_read(
        self,
        actor: Actor,
        obj: GovernedObject,
        command_context: CommandContext | None,
    ) -> bool:
        # Replacement semantics cannot grant permission to reveal diagnostics.
        if not self._types.supports_type_version(obj.type, obj.type_version):
            return False
        try:
            type_def = self._types.get(obj.type)
        except KeyError:
            return False
        auth_ctx, auth_deny = self._resolve_auth(command_context)
        if auth_deny is not None:
            return False
        decision = self._policy.evaluate(
            actor,
            "read",
            obj,
            type_def,
            application=auth_ctx.application if auth_ctx else None,
            mandate=auth_ctx.mandate if auth_ctx else None,
            resolved_mandate=auth_ctx.resolved_mandate if auth_ctx else None,
            purpose=auth_ctx.purpose if auth_ctx else None,
        )
        return decision.decision == Decision.ALLOW

    def _dispatch(
        self,
        command: Command,
        actor: Actor,
        command_context: CommandContext | None,
    ) -> CommandResult:
        if isinstance(command, CreateObject):
            return self._execute_create(command, actor, command_context)
        if isinstance(command, ReadObject):
            return self._execute_read(command, actor, command_context)
        if isinstance(command, AddRelationship):
            return self._execute_add_relationship(command, actor, command_context)
        if isinstance(command, RemoveRelationship):
            return self._execute_remove_relationship(command, actor, command_context)
        if isinstance(command, TransitionState) or command.object_id:
            return self._execute_transition(command, actor, command_context)
        return self._finalize(
            CommandResult(decision=Decision.DENY, reason="unknown command", action=command.action)
        )

    def get_object(self, object_id: str, *, raw: bool = False) -> GovernedObject | None:
        """Admin/stored view. Use materialize() for current canonical state."""
        if raw:
            return self._store.get(object_id)
        return self._materializer.materialize(
            object_id, self._context, hydrate=False, persist=False
        )

    def materialize(
        self,
        object_id: str,
        actor: Actor | None = None,
        *,
        field_filter: bool = False,
        fields: list[str] | None = None,
    ) -> GovernedObject | None:
        """Unified canonical materialization — hydrate + optional field filter."""
        try:
            return self._materializer.materialize(
                object_id,
                self._context,
                actor,
                hydrate=True,
                field_filter=field_filter,
                fields=fields,
                persist=True,
            )
        except ProviderError as exc:
            return None

    def replay(self, object_id: str, *, allow_legacy: bool = False) -> GovernedObject | None:
        """Stage supported reconstruction before installing state and edges.

        ReplayRefused is content-free; None means only a missing target.
        Legacy opt-in changes ordering, not binding requirements. Supported
        no-history/no-reducer/decline fallback is not reconstruction proof.
        """
        stored = self._store.get(object_id)
        if stored is None:
            return None
        if not self._replayer.check_type_version_supported(stored):
            raise ReplayRefused()
        obj = self._replayer.reconstruct(object_id, allow_legacy=allow_legacy)
        if obj is None:
            return None
        rels = self._replayer.reconstruct_relationships(object_id, allow_legacy=allow_legacy)
        self._replayer._check_result(stored, obj)
        if not self._replayer.check_type_version_supported(stored) or any(
            rel.source_id != object_id for rel in rels
        ):
            raise ReplayRefused()
        self._store.save(obj)
        replace_canonical_for_object(self._store, object_id, rels)
        return obj

    def canonical_relationships(self, object_id: str) -> list[Relationship]:
        return list_canonical_for_object(self._store, object_id)

    def add_relationship(
        self,
        object_id: str,
        relation: str,
        target_id: str,
        *,
        actor: Actor,
        target_type: str | None = None,
        command_context: CommandContext | None = None,
    ) -> CommandResult:
        return self.execute(
            AddRelationship(object_id, relation, target_id, target_type=target_type),
            actor=actor,
            command_context=command_context,
        )

    def remove_relationship(
        self,
        object_id: str,
        relation: str,
        target_id: str,
        *,
        actor: Actor,
        command_context: CommandContext | None = None,
    ) -> CommandResult:
        return self.execute(
            RemoveRelationship(object_id, relation, target_id),
            actor=actor,
            command_context=command_context,
        )

    def reconcile(self, object_id: str):
        return self._reconciliation.check(object_id, self._context)

    def reconcile_and_ingest(self, object_id: str):
        return self._reconciliation.reconcile_and_ingest(object_id, self._context)

    def ingest_observation(self, observation):
        return self._observations.ingest(observation)

    def resolve_review(self, case_id: str, *, resolution: str, resolved_by: str):
        return self._observations.resolve_review(
            case_id, resolution=resolution, resolved_by=resolved_by
        )

    @property
    def observation_ingestion(self) -> ObservationIngestionService:
        return self._observations

    @property
    def outbound(self) -> OutboundReconciliationService:
        return self._outbound

    def plan_repair(self, object_id: str, *, fields: list[str] | None = None):
        return self._outbound.plan(object_id, self._context, fields=fields)

    def apply_repair(self, plan, *, initiated_by: str = "system", simulate_outcome_unknown: bool = False):
        return self._outbound.apply(
            plan,
            self._context,
            initiated_by=initiated_by,
            simulate_outcome_unknown=simulate_outcome_unknown,
        )

    def replace_provider(self, object_id: str, *, new_provider_name: str, new_external_id: str):
        return self._outbound.replace_provider(
            object_id,
            new_provider_name=new_provider_name,
            new_external_id=new_external_id,
            context=self._context,
        )

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
        return self._query.query(
            actor,
            object_type=object_type,
            object_types=object_types,
            state=state,
            include_subtypes=include_subtypes,
            auth=auth,
        )

    def detach_provider(self, object_id: str) -> GovernedObject | None:
        return detach_from_provider(self._store, self._materializer, self._context, object_id)

    def history(self, object_id: str) -> list[GovernanceEvent]:
        return self._store.list_for_object(object_id)

    def accepted_history(self, object_id: str) -> list[GovernanceEvent]:
        """Accepted canonical changes in recorded per-object position order.

        EXPERIMENTAL. Selects rows carrying
        accepted-history positions, unlike timestamp-ordered ``history()``
        event/audit rows. Replay defaults to positioned accepted revisions;
        unpositioned accepted history requires explicit ``allow_legacy=True``
        for legacy timestamp ordering. Exact binding support still applies;
        supported no-history/no-reducer/decline fallbacks are not proof of
        reconstruction.
        """
        return self._store.list_accepted_for_object(object_id)

    def export_interchange_bundle(
        self,
        identities: list[str],
        *,
        actor: Actor,
        closed: bool,
        include_timestamps: bool = False,
        source_implementation: str | None = None,
    ) -> dict[str, Any]:
        """EXPERIMENTAL: export ``go-core-interchange-experiment/2``. Raises
        ``ExportRefused`` instead of emitting a repaired bundle."""
        from governed_objects.interchange.export import InterchangeExporter

        return InterchangeExporter(self, self._store, self._types, self._policy).export(
            identities,
            actor=actor,
            closed=closed,
            include_timestamps=include_timestamps,
            source_implementation=source_implementation,
        )

    def import_interchange_bundle(self, bundle, *, actor: Actor, provenance=None):
        """EXPERIMENTAL privileged new-target import.

        ``bundle`` is a semantic ``ImportBundle``. Restores accepted history as
        history in one transaction, or raises ``ImportRefused`` with the store
        unchanged. Requires the local import grant; historical actors are
        evidence only and are never authorized."""
        from governed_objects.interchange.importer import InterchangeImporter

        return InterchangeImporter(self._store, self._types, self._governance, self._clock).import_bundle(
            bundle, actor=actor, provenance=provenance
        )

    def prc_declaration(self) -> dict[str, Any]:
        """EXPERIMENTAL: this runtime's Draft 2.2 Portable Restoration Continuity
        role, capability, evidence-level, and handoff declaration for the export
        and import APIs above, with type bindings from the configured registry."""
        from governed_objects.interchange.prc import prc_declaration

        return prc_declaration(self._types)

    def interchange_imports(self) -> list[dict[str, Any]]:
        """EXPERIMENTAL: import provenance records (Operational Metadata)."""
        return self._store.list_imports()

    def related(
        self,
        actor: Actor,
        object_id: str,
        *,
        auth: AuthorizationContext | None = None,
    ) -> list[GovernedObject]:
        return self._query.related(actor, object_id, auth=auth)

    def traverse(
        self,
        actor: Actor,
        start_object_id: str,
        *,
        max_depth: int = 3,
        relations: list[str] | None = None,
        auth: AuthorizationContext | None = None,
    ) -> list[TraversalResult]:
        return self._traversal.traverse(
            actor,
            start_object_id,
            max_depth=max_depth,
            relations=relations,
            auth=auth,
        )

    def _finalize(self, result: CommandResult) -> CommandResult:
        if result.outcome_category is not None:
            return result
        if result.decision == Decision.DENY and result.deny_reason in REFUSAL_DENY_REASONS:
            return result.model_copy(update={"outcome_category": REFUSED})
        category = classify_outcome(
            result.decision,
            deny_reason=result.deny_reason,
            conflict_reason=result.conflict_reason,
            has_validation_checks=bool(result.validation_checks),
        )
        return result.model_copy(update={"outcome_category": category.value})

    def _execute_add_relationship(
        self,
        command: AddRelationship,
        actor: Actor,
        command_context: CommandContext | None = None,
    ) -> CommandResult:
        stored = self._store.get(command.object_id or "")
        if stored is None:
            return self._finalize(
                CommandResult(
                    decision=Decision.DENY,
                    reason="not found",
                    action="add_relationship",
                    deny_reason=DenyReason.NOT_FOUND.value,
                )
            )

        if not self._types.supports_type_version(stored.type, stored.type_version):
            return self._deny(
                stored.id,
                stored.type,
                actor,
                "add_relationship",
                "unsupported type version",
                stored.state,
                stored.state,
                deny_reason=DenyReason.TYPE_VERSION_UNSUPPORTED,
                command_context=command_context,
            )

        relation = command.payload.get("relation", "")
        target_id = command.payload.get("target_id", "")
        target_type = command.payload.get("target_type")
        if not relation or not target_id:
            return self._deny(
                stored.id,
                stored.type,
                actor,
                "add_relationship",
                "relationship requires type and target",
                stored.state,
                stored.state,
                deny_reason=DenyReason.RELATIONSHIP_INVALID,
                command_context=command_context,
            )

        type_def = self._types.get(stored.type)
        auth, auth_deny = self._resolve_auth(command_context)
        if auth_deny is not None:
            return auth_deny

        policy_decision = self._policy.evaluate(
            actor,
            "add_relationship",
            stored,
            type_def,
            application=auth.application if auth else None,
            mandate=auth.mandate if auth else None,
            resolved_mandate=auth.resolved_mandate if auth else None,
            purpose=auth.purpose if auth else None,
        )
        if policy_decision.decision == Decision.DENY:
            return self._deny(
                stored.id,
                stored.type,
                actor,
                "add_relationship",
                policy_decision.reason,
                stored.state,
                stored.state,
                deny_reason=policy_decision.deny_reason,
                command_context=command_context,
            )

        conflict = self._check_version_conflict(
            stored, "add_relationship", actor, command_context
        )
        if conflict is not None:
            return conflict

        existing = list_canonical_for_object(self._store, stored.id)
        prospective = existing if any(r.relation == relation and r.target_id == target_id for r in existing) else (
            existing + [canonical_relationship(stored.id, relation, target_id, target_type=target_type)]
        )
        admission = self._validate_mandate_candidate(stored, prospective, actor, 'add_relationship', command_context)
        if admission is not None:
            return admission
        if any(r.relation == relation and r.target_id == target_id for r in existing):
            return self._finalize(
                CommandResult(
                    decision=Decision.ALLOW,
                    reason="relationship already present",
                    action="add_relationship",
                    object_id=stored.id,
                    object_version=stored.version,
                )
            )

        with self._store.observation_transaction():
            current = self._store.get(stored.id)
            if current is None:
                return self._finalize(
                    CommandResult(
                        decision=Decision.DENY,
                        reason="not found",
                        action="add_relationship",
                        deny_reason=DenyReason.NOT_FOUND.value,
                    )
                )
            conflict = self._check_version_conflict(
                current, "add_relationship", actor, command_context
            )
            if conflict is not None:
                return conflict
            new_version = self._allocate_version(current)
            if new_version is None:
                return self._version_space_exhausted(current, actor, "add_relationship", command_context)
            rel = canonical_relationship(
                current.id, relation, target_id, target_type=target_type
            )
            updated = current.model_copy(update={"version": new_version})
            self._store.add(rel)
            self._store.save(updated)

            details = {
                "relation": relation,
                "target_id": target_id,
                "target_type": target_type,
            }
            event = self._allow_event(
                updated,
                actor,
                "add_relationship",
                current.state,
                current.state,
                "relationship added",
                command_context=command_context,
                extra_details=details,
            )
            event = self._store.append_accepted(event)
        return self._finalize(
            CommandResult(
                decision=Decision.ALLOW,
                reason="relationship added",
                action="add_relationship",
                object_id=updated.id,
                object_version=updated.version,
                events=[event],
            )
        )

    def _execute_remove_relationship(
        self,
        command: RemoveRelationship,
        actor: Actor,
        command_context: CommandContext | None = None,
    ) -> CommandResult:
        stored = self._store.get(command.object_id or "")
        if stored is None:
            return self._finalize(
                CommandResult(
                    decision=Decision.DENY,
                    reason="not found",
                    action="remove_relationship",
                    deny_reason=DenyReason.NOT_FOUND.value,
                )
            )

        type_def = bound_type_definition(self._types, stored)
        if type_def is None:
            return self._read_not_authorized(stored.id).model_copy(update={"action": command.action})

        relation = command.payload.get("relation", "")
        target_id = command.payload.get("target_id", "")
        if not relation or not target_id:
            return self._deny(
                stored.id,
                stored.type,
                actor,
                "remove_relationship",
                "relationship requires type and target",
                stored.state,
                stored.state,
                deny_reason=DenyReason.RELATIONSHIP_INVALID,
                command_context=command_context,
            )

        auth, auth_deny = self._resolve_auth(command_context)
        if auth_deny is not None:
            return auth_deny

        policy_decision = self._policy.evaluate(
            actor,
            "remove_relationship",
            stored,
            type_def,
            application=auth.application if auth else None,
            mandate=auth.mandate if auth else None,
            resolved_mandate=auth.resolved_mandate if auth else None,
            purpose=auth.purpose if auth else None,
        )
        if policy_decision.decision == Decision.DENY:
            return self._deny(
                stored.id,
                stored.type,
                actor,
                "remove_relationship",
                policy_decision.reason,
                stored.state,
                stored.state,
                deny_reason=policy_decision.deny_reason,
                command_context=command_context,
            )

        conflict = self._check_version_conflict(
            stored, "remove_relationship", actor, command_context
        )
        if conflict is not None:
            return conflict

        existing = [
            r
            for r in self._store.list_for_source(stored.id)
            if is_canonical(r) and r.relation == relation and r.target_id == target_id
        ]
        if not existing:
            return self._finalize(
                CommandResult(
                    decision=Decision.ALLOW,
                    reason="relationship not present",
                    action="remove_relationship",
                    object_id=stored.id,
                    object_version=stored.version,
                )
            )

        prospective = [r for r in list_canonical_for_object(self._store, stored.id)
                       if not (r.relation == relation and r.target_id == target_id)]
        admission = self._validate_mandate_candidate(stored, prospective, actor, 'remove_relationship', command_context)
        if admission is not None:
            return admission

        with self._store.observation_transaction():
            current = self._store.get(stored.id)
            if current is None:
                return self._finalize(
                    CommandResult(
                        decision=Decision.DENY,
                        reason="not found",
                        action="remove_relationship",
                        deny_reason=DenyReason.NOT_FOUND.value,
                    )
                )
            conflict = self._check_version_conflict(
                current, "remove_relationship", actor, command_context
            )
            if conflict is not None:
                return conflict
            new_version = self._allocate_version(current)
            if new_version is None:
                return self._version_space_exhausted(
                    current, actor, "remove_relationship", command_context
                )
            self._store.remove(current.id, relation, target_id)
            updated = current.model_copy(update={"version": new_version})
            self._store.save(updated)

            details = {"relation": relation, "target_id": target_id}
            event = self._allow_event(
                updated,
                actor,
                "remove_relationship",
                current.state,
                current.state,
                "relationship removed",
                command_context=command_context,
                extra_details=details,
            )
            event = self._store.append_accepted(event)
        return self._finalize(
            CommandResult(
                decision=Decision.ALLOW,
                reason="relationship removed",
                action="remove_relationship",
                object_id=updated.id,
                object_version=updated.version,
                events=[event],
            )
        )

    def _execute_create(
        self,
        command: CreateObject,
        actor: Actor,
        command_context: CommandContext | None = None,
    ) -> CommandResult:
        if command.object_type == 'DelegatedMandate':
            try:
                self.require_delegation()
            except DelegationConfigurationError:
                return self._finalize(CommandResult(
                    decision=Decision.DENY, reason='mandate authority unavailable',
                    deny_reason=DenyReason.MANDATE_INVALID.value,
                ))
            if (not isinstance(command.payload.get('data'), dict)
                    or (command.payload.get('state') is not None
                        and command.payload['state'] not in ('draft', 'active', 'revoked'))):
                return self._finalize(CommandResult(
                    decision=Decision.DENY, reason='mandate authority unavailable',
                    deny_reason=DenyReason.MANDATE_INVALID.value,
                ))
        type_def = self._types.get(command.object_type or "")
        data = dict(command.payload.get("data", {}))
        canonicalizer = self._extensions.canonicalizer_for(
            command.object_type or "", self._types
        )
        if canonicalizer is not None:
            data = canonicalizer.canonicalize_create(data)
        state = command.payload.get("state") or type_def.default_state()
        relationships = command.payload.get("relationships", [])

        auth, auth_deny = self._resolve_auth(command_context)
        if auth_deny is not None:
            return auth_deny

        policy_decision = self._policy.evaluate(
            actor,
            "create",
            None,
            type_def,
            application=auth.application if auth else None,
            mandate=auth.mandate if auth else None,
            resolved_mandate=auth.resolved_mandate if auth else None,
            purpose=auth.purpose if auth else None,
        )
        if policy_decision.decision == Decision.DENY:
            return self._deny(
                command.object_id or "new",
                command.object_type or "",
                actor,
                "create",
                policy_decision.reason,
                None,
                state,
                deny_reason=policy_decision.deny_reason,
                command_context=command_context,
            )

        if command.object_id and self._store.identities_present([command.object_id]):
            return self._finalize(
                CommandResult(
                    decision=Decision.DENY,
                    reason="object identity already exists",
                    action="create",
                    object_id=command.object_id,
                    deny_reason=DenyReason.IDENTITY_EXISTS.value,
                )
            )

        adapter = self._adapters.for_type(command.object_type or "")
        checks: list[ValidationCheck] = []
        if adapter:
            valid, checks, reason = adapter.validate_create(type_def, data)
            if not valid:
                return self._deny(
                    command.object_id or "new",
                    command.object_type or "",
                    actor,
                    "create",
                    reason,
                    None,
                    state,
                    checks,
                    deny_reason=DenyReason.SCHEMA_INVALID,
                )
        else:
            valid, checks, errors = self._schema.validate(type_def, data)
            if not valid:
                return self._deny(
                    command.object_id or "new",
                    command.object_type or "",
                    actor,
                    "create",
                    "; ".join(errors),
                    None,
                    state,
                    checks,
                    deny_reason=DenyReason.SCHEMA_INVALID,
                )

        # A type with no configured reference is created exactly as before and
        # stores none; only export requires one.
        governance_reference = (
            self._governance.reference_for(type_def.kind, type_def.type_version)
            if self._governance is not None
            else None
        )

        try:
            canonical_rels = [
                canonical_relationship(
                    rel_data.get("source_id", command.object_id or ""),
                    rel_data["relation"], rel_data["target_id"], target_type=rel_data.get("target_type"),
                ) for rel_data in relationships
            ]
        except (KeyError, ValueError, TypeError, AttributeError):
            if command.object_type != 'DelegatedMandate':
                raise
            return self._finalize(CommandResult(
                decision=Decision.DENY, reason='mandate authority unavailable',
                deny_reason=DenyReason.MANDATE_INVALID.value,
            ))

        obj = GovernedObject(
            id=command.object_id or "",
            type=command.object_type or "",
            type_version=type_def.type_version,
            version=1,
            state=state,
            data=data,
            metadata=command.payload.get("metadata", {}),
            governance_reference=governance_reference,
        )
        admission = self._validate_mandate_candidate(obj, canonical_rels, actor, 'create', command_context)
        if admission is not None:
            return admission

        rel_details = [
            {
                "source_id": r.source_id,
                "relation": r.relation,
                "target_id": r.target_id,
                "target_type": r.target_type,
            }
            for r in canonical_rels
        ]
        create_details: dict[str, Any] = {}
        if rel_details:
            create_details["relationships"] = rel_details
        if governance_reference is not None:
            create_details["governance_reference"] = governance_reference

        with self._store.observation_transaction():
            if command.object_id and self._store.identities_present([command.object_id]):
                return self._finalize(
                    CommandResult(
                        decision=Decision.DENY,
                        reason="object identity already exists",
                        action="create",
                        object_id=command.object_id,
                        deny_reason=DenyReason.IDENTITY_EXISTS.value,
                    )
                )
            self._store.save(obj)

            for rel in canonical_rels:
                self._store.add(rel)

            event = self._allow_event(
                obj,
                actor,
                "create",
                None,
                state,
                "object created",
                checks,
                data=data,
                command_context=command_context,
                extra_details=create_details or None,
            )
            event = self._store.append_accepted(event)
        return self._finalize(
            CommandResult(
                decision=Decision.ALLOW,
                reason="object created",
                action="create",
                object_id=obj.id,
                new_state=state,
                object_version=1,
                events=[event],
                validation_checks=checks,
                data=obj.data,
            )
        )

    def _execute_read(
        self,
        command: ReadObject,
        actor: Actor,
        command_context: CommandContext | None = None,
    ) -> CommandResult:
        # Authority is established before any provider interaction, so a
        # requester without read authority never observes a provider outcome
        # (AUTH-ORDER-001, AUTH-DISCLOSE-001).
        stored = self._store.get(command.object_id or "")
        if stored is not None:
            denied = self._read_denial(stored, actor, command_context)
            if denied is not None:
                return denied
        try:
            obj = self._materializer.materialize(
                command.object_id or "",
                self._context,
                actor,
                hydrate=True,
                field_filter=False,
                persist=True,
            )
        except ProviderError as exc:
            stored = self._store.get(command.object_id or "")
            return self._provider_failure(
                command.object_id or "",
                stored.type if stored else "Unknown",
                actor,
                "read",
                exc,
            )

        if obj is None:
            return self._finalize(
                CommandResult(
                    decision=Decision.DENY,
                    reason="not found",
                    action="read",
                    deny_reason=DenyReason.NOT_FOUND.value,
                )
            )

        type_def = self._types.get(obj.type)
        denied = self._read_denial(obj, actor, command_context)
        if denied is not None:
            return denied

        adapter = self._adapters.resolve(obj)
        fields = command.payload.get("fields") or []
        data, denied_fields = adapter.filter_read_data(obj, type_def, actor, fields)

        if denied_fields and not data:
            return self._deny(
                obj.id,
                obj.type,
                actor,
                "read",
                "access denied",
                obj.state,
                obj.state,
            )

        reason = "read permitted"
        if denied_fields:
            reason = "partial read"

        event = self._allow_event(
            obj, actor, "read", obj.state, obj.state, reason, command_context=command_context
        )
        self._store.append(event)
        return self._finalize(
            CommandResult(
                decision=Decision.ALLOW,
                reason=reason,
                action="read",
                object_id=obj.id,
                object_version=obj.version,
                events=[event],
                data=data,
            )
        )

    def _read_not_authorized(self, object_id: str) -> CommandResult:
        """Local policy: identical unreadable-target surface, no audit payload."""
        return self._finalize(CommandResult(
            decision=Decision.DENY,
            reason="access denied",
            action="read",
            object_id=object_id,
            deny_reason=DenyReason.READ_NOT_AUTHORIZED.value,
        ))

    def _read_denial(
        self,
        obj: GovernedObject,
        actor: Actor,
        command_context: CommandContext | None,
    ) -> CommandResult | None:
        # Resolve independent context errors identically for either binding.
        auth, auth_deny = self._resolve_auth(command_context)
        if auth_deny is not None:
            if auth_deny.deny_reason == DenyReason.MANDATE_INVALID.value:
                return self._read_not_authorized(obj.id)
            return auth_deny
        if not self._types.supports_type_version(obj.type, obj.type_version):
            return self._read_not_authorized(obj.id)
        type_def = self._types.get(obj.type)
        policy_decision = self._policy.evaluate(
            actor,
            "read",
            obj,
            type_def,
            application=auth.application if auth else None,
            mandate=auth.mandate if auth else None,
            resolved_mandate=auth.resolved_mandate if auth else None,
            purpose=auth.purpose if auth else None,
        )
        if policy_decision.decision == Decision.DENY:
            return self._read_not_authorized(obj.id)
        return None

    def _execute_transition(
        self,
        command: Command,
        actor: Actor,
        command_context: CommandContext | None = None,
    ) -> CommandResult:
        stored = self._store.get(command.object_id or "")
        if stored is None:
            return self._finalize(
                CommandResult(
                    decision=Decision.DENY,
                    reason="not found",
                    action=command.action,
                    deny_reason=DenyReason.NOT_FOUND.value,
                )
            )

        type_def = bound_type_definition(self._types, stored)
        if type_def is None:
            return self._read_not_authorized(stored.id).model_copy(update={"action": command.action})
        action = command.action

        auth, auth_deny = self._resolve_auth(command_context)
        if auth_deny is not None:
            return auth_deny

        policy_decision = self._policy.evaluate(
            actor,
            action,
            stored,
            type_def,
            application=auth.application if auth else None,
            mandate=auth.mandate if auth else None,
            resolved_mandate=auth.resolved_mandate if auth else None,
            purpose=auth.purpose if auth else None,
        )
        if policy_decision.decision == Decision.DENY:
            return self._deny(
                stored.id,
                stored.type,
                actor,
                action,
                policy_decision.reason,
                stored.state,
                stored.state,
                deny_reason=policy_decision.deny_reason,
                command_context=command_context,
            )

        candidate = stored.model_copy(update={'state': type_def.transition_target(stored.state, action) or stored.state})
        admission = self._validate_mandate_candidate(
            candidate, list_canonical_for_object(self._store, stored.id), actor, action, command_context,
        )
        if admission is not None:
            return admission

        # A cached result is only returned to a requester already authorized
        # for this operation; it precedes the version check so a retry of an
        # accepted command still returns its original result.
        if command_context and command_context.idempotency_key:
            cached = self._idempotency.get(
                command.object_id or "", command_context.idempotency_key
            )
            if cached is not None:
                return self._finalize(cached)

        conflict = self._check_version_conflict(
            stored, action, actor, command_context
        )
        if conflict is not None:
            return conflict

        ctx = AdapterContext(
            self._store,
            self._providers,
            self._types,
            command_context=command_context,
        )
        try:
            obj = self._materializer.materialize(
                command.object_id or "",
                ctx,
                hydrate=True,
                persist=False,
            )
        except ProviderError as exc:
            return self._provider_failure(
                command.object_id or "",
                stored.type,
                actor,
                command.action,
                exc,
            )

        if obj is None:
            return self._finalize(
                CommandResult(
                    decision=Decision.DENY,
                    reason="not found",
                    action=command.action,
                    deny_reason=DenyReason.NOT_FOUND.value,
                )
            )

        ok, lifecycle_reason, lifecycle_check = self._lifecycle.validate_transition(
            type_def, stored.state, action
        )
        checks = [lifecycle_check]
        if not ok:
            return self._deny(
                obj.id,
                obj.type,
                actor,
                action,
                lifecycle_reason,
                obj.state,
                obj.state,
                checks,
                deny_reason=DenyReason.LIFECYCLE_INVALID,
                command_context=command_context,
            )

        adapter = self._adapters.resolve(obj)
        domain_ok, domain_checks, domain_reason = adapter.validate_transition(
            obj, type_def, action, actor
        )
        checks.extend(domain_checks)
        if not domain_ok:
            return self._deny(
                obj.id,
                obj.type,
                actor,
                action,
                domain_reason,
                obj.state,
                obj.state,
                checks,
                deny_reason=DenyReason.OBJECT_POLICY_DENIED,
                command_context=command_context,
            )

        new_version = self._allocate_version(obj)
        if new_version is None:
            return self._version_space_exhausted(obj, actor, action, command_context)
        previous_state = obj.state
        adapter_result = adapter.adapter_execute(obj, command, ctx)

        if adapter_result is not None:
            if not adapter_result.success:
                if adapter_result.failure == AdapterFailure.INVALID:
                    return self._adapter_rejected(
                        obj, actor, action, adapter_result, checks, command_context
                    )
                if adapter_result.failure == AdapterFailure.PROVIDER:
                    err = ProviderError(
                        adapter_result.error_message or "provider failed",
                        code=adapter_result.error_code or ProviderError.code,
                    )
                    return self._provider_failure(
                        obj.id, obj.type, actor, action, err, checks=checks
                    )
                return self._adapter_result_invalid(
                    obj, actor, action, "adapter failure has no failure classification",
                    checks, command_context, adapter_result=adapter_result,
                )
            if adapter_result.failure is not None:
                return self._adapter_result_invalid(
                    obj, actor, action, "adapter reported success with a failure classification",
                    checks, command_context, adapter_result=adapter_result,
                )
            proposed = adapter_result.object
            if proposed is None:
                return self._adapter_result_invalid(
                    obj, actor, action, "adapter reported success without a resulting object",
                    checks, command_context,
                )
            # The runtime alone issues Object Versions; any adapter-proposed
            # version is discarded. An accepted transition is a revision even
            # when its resulting values equal the prior ones.
            obj = proposed.model_copy(update={"version": new_version})
            if adapter_result.relationships:
                binding = obj.metadata.get("provider_binding", {})
                pname = binding.get("provider_name") or binding.get("provider")
                if pname:
                    self._rel_sync.sync_provider_relationships(
                        obj.id, adapter_result.relationships, provider_name=pname
                    )
            if command_context and command_context.correlation_id:
                obj = self._observations.record_governed_echo(
                    obj,
                    correlation_id=command_context.correlation_id,
                )
            new_state = obj.state
        else:
            new_state = type_def.transition_target(obj.state, action)
            if new_state is None:
                return self._deny(
                    obj.id,
                    obj.type,
                    actor,
                    action,
                    f"no transition for {action} from {obj.state}",
                    obj.state,
                    obj.state,
                    checks,
                )
            obj = obj.model_copy(update={"state": new_state, "version": new_version})

        with self._store.observation_transaction():
            current = self._store.get(obj.id)
            if current is None:
                return self._finalize(
                    CommandResult(
                        decision=Decision.DENY,
                        reason="not found",
                        action=action,
                        deny_reason=DenyReason.NOT_FOUND.value,
                    )
                )
            if current.version != stored.version:
                return self._conflict(
                    current,
                    actor,
                    action,
                    expected_version=stored.version,
                    actual_version=current.version,
                    command_context=command_context,
                )
            self._store.save(obj)

            event = self._allow_event(
                obj,
                actor,
                action,
                previous_state,
                new_state,
                "transition applied",
                checks,
                command_context=command_context,
                extra_details={
                    "accepted_post_image": {"data": dict(obj.data), "state": new_state}
                },
            )
            event = self._store.append_accepted(event)
        result = self._finalize(
            CommandResult(
                decision=Decision.ALLOW,
                reason="transition applied",
                action=action,
                object_id=obj.id,
                previous_state=previous_state,
                new_state=new_state,
                object_version=obj.version,
                events=[event],
                validation_checks=checks,
                data=obj.data,
            )
        )
        if command_context and command_context.idempotency_key:
            self._idempotency.put(
                obj.id,
                command_context.idempotency_key,
                result,
                command_id=command_context.command_id,
                correlation_id=command_context.correlation_id,
            )
        return result

    def require_delegation(self):
        if self._delegation is None:
            raise DelegationConfigurationError()
        self._delegation.validate(self._types, self._adapters, self._extensions)
        return self._delegation

    def _decode_legacy_scope(self, scoped_to, store):
        return self.require_delegation().scope_decoder(scoped_to, store)

    def _validate_delegation_binding(self, obj):
        configuration = self.require_delegation()
        configuration.validate(self._types, self._adapters, self._extensions, obj)
        mandate = materialize_mandate(obj, self._store, scope_decoder=configuration.scope_decoder)
        configuration.actor(mandate.principal_id)
        configuration.actor(mandate.delegate_actor_id)

    def _validate_mandate_candidate(self, obj, relationships, actor, action, command_context):
        if obj.type != 'DelegatedMandate':
            return None
        try:
            configuration = self.require_delegation()
            configuration.validate(self._types, self._adapters, self._extensions, obj)
            mandate = materialize_mandate(obj, self._store, scope_decoder=configuration.scope_decoder,
                                         relationships=relationships)
            principal = configuration.actor(mandate.principal_id)
            configuration.actor(mandate.delegate_actor_id)
            if action != 'revoke':
                parent = None
                ancestors = frozenset()
                if mandate.parent_mandate_id:
                    if mandate.parent_mandate_id == obj.id:
                        raise ValueError('delegation cycle')
                    parent = self._mandate_resolver.resolve_authority(mandate.parent_mandate_id)
                    if parent is None or not parent.is_effective_at(self._clock.now())[0]:
                        raise ValueError('ineffective parent')
                    ancestors = frozenset(parent.parent_mandate_ids)
                    if obj.id in ancestors:
                        raise ValueError('delegation cycle')
                validation = self._delegation_validator.validate(
                    principal=principal,
                    spec=DelegationSpec(mandate.capabilities, mandate.scope, mandate.purposes,
                                        mandate.valid_from, mandate.valid_until, mandate.parent_mandate_id, mandate.id),
                    parent_mandate=parent, ancestor_ids=ancestors,
                    max_depth=self._mandate_resolver.MAX_CHAIN_DEPTH+1,
                )
                if not validation.valid:
                    raise ValueError('invalid delegation')
        except (ValueError, TypeError, KeyError, AttributeError):
            return self._deny(obj.id, obj.type, actor, action, 'mandate authority unavailable',
                              obj.state, obj.state, deny_reason=DenyReason.MANDATE_INVALID,
                              command_context=command_context)
        return None

    def resolve_authorization_context(
        self, auth: AuthorizationContext | None
    ) -> AuthorizationContext | None:
        """Re-resolve referenced bounds; snapshots are identities, not grants."""
        if auth is None:
            return None
        if (not isinstance(auth, AuthorizationContext)
                or (auth.application is not None and not isinstance(auth.application, ApplicationDefinition))
                or (auth.mandate is not None and not isinstance(auth.mandate, DelegatedMandate))
                or (auth.resolved_mandate is not None and not isinstance(auth.resolved_mandate, ResolvedMandateAuthority))):
            raise AuthorizationContextError()
        app_values = (auth.application_id, auth.application.id if auth.application else None)
        mandate_values = (
            auth.mandate_id, auth.mandate.id if auth.mandate else None,
            auth.resolved_mandate.mandate_id if auth.resolved_mandate else None,
        )
        if any(value is not None and (not isinstance(value, str) or not value)
               for value in (*app_values, *mandate_values)):
            raise AuthorizationContextError()
        app_ids = {value for value in app_values if value is not None}
        mandate_ids = {value for value in mandate_values if value is not None}
        if len(app_ids) > 1 or len(mandate_ids) > 1:
            raise AuthorizationContextError()
        if auth.purpose is not None and (
            not isinstance(auth.purpose, str) or not auth.purpose
        ):
            raise AuthorizationContextError()
        current, deny = self._resolve_auth(CommandContext(
            application_id=next(iter(app_ids), None),
            mandate_id=next(iter(mandate_ids), None), purpose=auth.purpose,
        ))
        if deny is not None:
            raise AuthorizationContextError()
        return current

    def _resolve_auth(
        self, command_context: CommandContext | None
    ) -> tuple[AuthorizationContext | None, CommandResult | None]:
        if command_context is not None:
            for value, reason in ((command_context.application_id, DenyReason.APPLICATION_UNKNOWN),
                                  (command_context.mandate_id, DenyReason.MANDATE_INVALID),
                                  (command_context.purpose, DenyReason.MANDATE_INVALID)):
                if value is not None and (not isinstance(value, str) or not value):
                    return None, self._finalize(CommandResult(
                        decision=Decision.DENY, reason='authorization context unavailable', deny_reason=reason.value,
                    ))
        app, app_deny = self._resolve_application(command_context)
        if app_deny is not None:
            return None, app_deny
        mandate, resolved, mandate_deny = self._resolve_mandate(command_context)
        if mandate_deny is not None:
            return None, mandate_deny
        purpose = command_context.purpose if command_context else None
        if app is None and mandate is None and purpose is None:
            return None, None
        return (
            AuthorizationContext(
                application=app,
                mandate=mandate,
                resolved_mandate=resolved,
                purpose=purpose,
                application_id=command_context.application_id if command_context else None,
                mandate_id=command_context.mandate_id if command_context else None,
            ),
            None,
        )

    def _resolve_mandate(
        self, command_context: CommandContext | None
    ) -> tuple[Any | None, Any | None, CommandResult | None]:
        if command_context is None or not command_context.mandate_id:
            return None, None, None
        try:
            resolved = self._mandate_resolver.resolve_authority(command_context.mandate_id)
            mandate = self._mandate_resolver.resolve(command_context.mandate_id) if resolved else None
        except (ValueError, TypeError, KeyError, AttributeError):
            return None, None, self._finalize(CommandResult(
                decision=Decision.DENY, reason='mandate authority unavailable',
                deny_reason=DenyReason.MANDATE_INVALID.value,
            ))
        if resolved is None:
            return None, None, self._finalize(
                CommandResult(
                    decision=Decision.DENY,
                    reason=f"unknown mandate: {command_context.mandate_id}",
                    deny_reason=DenyReason.MANDATE_UNKNOWN.value,
                )
            )
        return mandate, resolved, None

    def _resolve_application(
        self, command_context: CommandContext | None
    ) -> tuple[ApplicationDefinition | None, CommandResult | None]:
        if command_context is None or not command_context.application_id:
            return None, None
        app = self._applications.get(command_context.application_id)
        if app is None:
            return None, self._finalize(
                CommandResult(
                    decision=Decision.DENY,
                    reason=f"unknown application: {command_context.application_id}",
                    deny_reason=DenyReason.APPLICATION_UNKNOWN.value,
                )
            )
        return app, None

    def _allocate_version(self, obj: GovernedObject) -> int | None:
        """Next unused Object Version, or None when the version space is exhausted."""
        return self._store.next_object_version(obj)

    def _version_space_exhausted(
        self,
        obj: GovernedObject,
        actor: Actor,
        action: str,
        command_context: CommandContext | None,
    ) -> CommandResult:
        result = self._deny(
            obj.id,
            obj.type,
            actor,
            action,
            "object version space exhausted",
            obj.state,
            obj.state,
            deny_reason=DenyReason.OBJECT_VERSION_SPACE_EXHAUSTED,
            command_context=command_context,
        )
        return result.model_copy(update={"outcome_category": REFUSED})

    def _adapter_result_invalid(
        self,
        obj: GovernedObject,
        actor: Actor,
        action: str,
        reason: str,
        checks: list[ValidationCheck],
        command_context: CommandContext | None,
        *,
        adapter_result: AdapterExecuteResult | None = None,
    ) -> CommandResult:
        """The adapter broke its execute contract; nothing canonical is accepted."""
        extra = None
        if adapter_result is not None:
            extra = {
                "adapter_error": adapter_result.error_code,
                "message": adapter_result.error_message,
                "adapter_success": adapter_result.success,
                "adapter_failure": adapter_result.failure,
            }
        result = self._deny(
            obj.id,
            obj.type,
            actor,
            action,
            reason,
            obj.state,
            obj.state,
            checks,
            deny_reason=DenyReason.ADAPTER_RESULT_INVALID,
            command_context=command_context,
            extra_details=extra,
        )
        return result.model_copy(update={"outcome_category": REFUSED})

    def _check_version_conflict(
        self,
        stored: GovernedObject,
        action: str,
        actor: Actor,
        command_context: CommandContext | None,
    ) -> CommandResult | None:
        if command_context is None or command_context.expected_version is None:
            return None
        if stored.version == command_context.expected_version:
            return None
        return self._conflict(
            stored,
            actor,
            action,
            expected_version=command_context.expected_version,
            actual_version=stored.version,
            command_context=command_context,
        )

    def _conflict(
        self,
        obj: GovernedObject,
        actor: Actor,
        action: str,
        *,
        expected_version: int,
        actual_version: int,
        command_context: CommandContext | None = None,
    ) -> CommandResult:
        details: dict = {
            "expected_version": expected_version,
            "actual_version": actual_version,
            "conflict_reason": ConflictReason.VERSION_MISMATCH.value,
        }
        if command_context is not None:
            meta = self._context_metadata(command_context)
            if meta is not None:
                details["command_context"] = meta
        event = GovernanceEvent(
            object_id=obj.id,
            object_type=obj.type,
            actor_id=actor.id,
            actor_type=actor.type.value,
            action=action,
            decision=Decision.CONFLICT,
            event_type=f"object.{action}.conflict",
            previous_state=obj.state,
            new_state=obj.state,
            reason="version conflict",
            object_version=actual_version,
            details=details,
        )
        self._store.append(event)
        return self._finalize(
            CommandResult(
                decision=Decision.CONFLICT,
                reason="version conflict",
                conflict_reason=ConflictReason.VERSION_MISMATCH.value,
                expected_version=expected_version,
                actual_version=actual_version,
                action=action,
                object_id=obj.id,
                previous_state=obj.state,
                new_state=obj.state,
                object_version=actual_version,
                events=[event],
            )
        )

    def _context_metadata(self, command_context: CommandContext | None) -> dict | None:
        if command_context is None:
            return None
        meta = command_context.to_metadata()
        if command_context.mandate_id:
            mandate = self._mandate_resolver.resolve(command_context.mandate_id)
            if mandate is not None:
                meta["principal_id"] = mandate.principal_id
        return meta

    def _provider_failure(
        self,
        object_id: str,
        object_type: str,
        actor: Actor,
        action: str,
        exc: ProviderError,
        checks: list[ValidationCheck] | None = None,
    ) -> CommandResult:
        """The backing provider failed on an otherwise evaluable request.

        No minimum outcome class describes this condition, so it is REFUSED
        (OP-OUTCOME-001); nothing canonical changes (OP-NOCHANGE-001)."""
        obj = self._store.get(object_id)
        version = obj.version if obj else 1
        state = obj.state if obj else None
        reason = exc.code
        events: list[GovernanceEvent] = []
        if action != "read":
            event = GovernanceEvent(
                object_id=object_id,
                object_type=object_type,
                actor_id=actor.id,
                actor_type=actor.type.value,
                action=action,
                decision=Decision.DENY,
                event_type=f"object.{action}.provider_failed",
                previous_state=state,
                new_state=state,
                reason=reason,
                object_version=version,
                details={
                    "provider_error": exc.code,
                    "message": exc.message,
                    "deny_reason": DenyReason.PROVIDER_FAILED.value,
                },
            )
            self._store.append(event)
            events = [event]
        return self._finalize(
            CommandResult(
                decision=Decision.DENY,
                reason=reason,
                deny_reason=DenyReason.PROVIDER_FAILED.value,
                action=action,
                object_id=object_id,
                events=events,
                validation_checks=checks or [],
            )
        )

    def _adapter_rejected(
        self,
        obj: GovernedObject,
        actor: Actor,
        action: str,
        result: AdapterExecuteResult,
        checks: list[ValidationCheck],
        command_context: CommandContext | None,
    ) -> CommandResult:
        """The type's adapter rejected the requested change under the type's
        declared rules: SEMANTICALLY_INVALID (TYPE-VALID-001)."""
        message = result.error_message or "change rejected by type rules"
        return self._deny(
            obj.id,
            obj.type,
            actor,
            action,
            result.error_code or message,
            obj.state,
            obj.state,
            [*checks, ValidationCheck(name="type_state", passed=False, message=message)],
            deny_reason=DenyReason.STATE_INVALID,
            command_context=command_context,
            extra_details={"adapter_error": result.error_code, "message": message},
        )

    def _deny(
        self,
        object_id: str,
        object_type: str,
        actor: Actor,
        action: str,
        reason: str,
        previous_state: str | None,
        new_state: str | None,
        checks: list[ValidationCheck] | None = None,
        *,
        deny_reason: DenyReason | None = None,
        command_context: CommandContext | None = None,
        extra_details: dict | None = None,
    ) -> CommandResult:
        obj = self._store.get(object_id)
        version = obj.version if obj else 1
        details: dict = dict(extra_details or {})
        if deny_reason is not None:
            details["deny_reason"] = deny_reason.value
        if command_context is not None:
            meta = self._context_metadata(command_context)
            if meta is not None:
                details["command_context"] = meta
        event = GovernanceEvent(
            object_id=object_id,
            object_type=object_type,
            actor_id=actor.id,
            actor_type=actor.type.value,
            action=action,
            decision=Decision.DENY,
            event_type=f"object.{action}.denied",
            previous_state=previous_state,
            new_state=new_state or previous_state,
            reason=reason,
            object_version=version,
            details=details,
        )
        self._store.append(event)
        return self._finalize(
            CommandResult(
                decision=Decision.DENY,
                reason=reason,
                deny_reason=deny_reason.value if deny_reason else None,
                action=action,
                object_id=object_id,
                previous_state=previous_state,
                new_state=new_state or previous_state,
                object_version=version,
                events=[event],
                validation_checks=checks or [],
            )
        )

    def _allow_event(
        self,
        obj: GovernedObject,
        actor: Actor,
        action: str,
        previous_state: str | None,
        new_state: str | None,
        reason: str,
        checks: list[ValidationCheck] | None = None,
        *,
        data: dict | None = None,
        command_context: CommandContext | None = None,
        extra_details: dict | None = None,
    ) -> GovernanceEvent:
        details: dict = {"validation_checks": [c.model_dump() for c in (checks or [])]}
        if data is not None:
            details["data"] = data
        if extra_details:
            details.update(extra_details)
        meta = self._context_metadata(command_context)
        if meta is not None:
            details["command_context"] = meta
        return GovernanceEvent(
            object_id=obj.id,
            object_type=obj.type,
            type_version=obj.type_version,
            actor_id=actor.id,
            actor_type=actor.type.value,
            action=action,
            decision=Decision.ALLOW,
            event_type=f"object.{action}",
            previous_state=previous_state,
            new_state=new_state,
            reason=reason,
            object_version=obj.version,
            details=details,
        )
