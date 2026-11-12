# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Brafford Labs, LLC
"""Outbound reconciliation — bring supported providers into canonical conformity."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from governed_objects.adapters.context import AdapterContext
from governed_objects.adapters.registry import AdapterRegistry
from governed_objects.authority.rules import AuthorityMode, AuthorityRuleResolver
from governed_objects.domain.models import GovernedObject
from governed_objects.outbound.models import (
    ProviderRepairPlan,
    ProviderRepairRequest,
    ProviderRepairResult,
    RepairStatus,
)
from governed_objects.outbound.store import RepairStore
from governed_objects.persistence.sqlite import SQLiteStore
from governed_objects.providers.binding import ProviderBinding
from governed_objects.providers.projection import ProviderProjection
from governed_objects.providers.errors import (
    ProviderConflict,
    ProviderError,
    ProviderObjectNotFound,
    ProviderOutcomeUnknown,
    ProviderProjectionUnsupported,
    ProviderUnavailable,
)
from governed_objects.providers.registry import ProviderRegistry
from governed_objects.types.registry import TypeRegistry


class OutboundReconciliationService:
    """Plan and apply provider repairs derived only from canonical accepted state."""

    def __init__(
        self,
        store: SQLiteStore,
        adapters: AdapterRegistry,
        providers: ProviderRegistry,
        type_registry: TypeRegistry,
        repair_store: RepairStore | None = None,
    ) -> None:
        self._store = store
        self._adapters = adapters
        self._providers = providers
        self._types = type_registry
        self._authority = AuthorityRuleResolver(type_registry)
        self._repairs = repair_store or RepairStore(store)

    @property
    def repair_store(self) -> RepairStore:
        return self._repairs

    def plan(
        self,
        object_id: str,
        context: AdapterContext,
        *,
        originating_observation_id: str | None = None,
        fields: list[str] | None = None,
    ) -> ProviderRepairPlan | None:
        stored, provider_view, binding, projection, token, provider_version = self._load_views(object_id, context)
        if stored is None or binding is None or provider_view is None:
            return None

        changes: dict[str, Any] = {}
        repair_fields: list[str] = []
        drifts: list[str] = []
        unrepairable: list[str] = []
        projection_gaps: list[str] = []
        caps = projection.capabilities

        candidate_fields = fields or self._drift_fields(stored, provider_view)
        explicit_fields = fields is not None
        for field_name in candidate_fields:
            canonical_val = stored.state if field_name == "state" else stored.data.get(field_name)
            provider_val = provider_view.state if field_name == "state" else provider_view.data.get(field_name)
            if canonical_val == provider_val:
                continue
            drifts.append(f"{field_name}: canonical={canonical_val} provider={provider_val}")
            mode = self._authority.mode_for_field(stored.type, field_name)
            if not explicit_fields and mode != AuthorityMode.CANONICAL_AUTHORITATIVE:
                continue
            if not caps.can_write(field_name):
                raise ProviderProjectionUnsupported("UNWRITABLE_PROVIDER_FIELD")
            if field_name not in caps.representable_fields:
                raise ProviderProjectionUnsupported("UNREPRESENTABLE_PROVIDER_FIELD")
            changes[field_name] = canonical_val
            repair_fields.append(field_name)

        if not changes and not drifts:
            return None

        return ProviderRepairPlan(
            object_id=object_id,
            provider_name=binding.provider_name,
            external_id=binding.external_id or object_id,
            type_identity=stored.type,
            type_version=stored.type_version,
            projection_token=token,
            canonical_expected_version=stored.version,
            provider_expected_version=provider_version,
            changes=changes,
            drifts=drifts,
            repair_fields=repair_fields,
            projection_gaps=projection_gaps,
            unrepairable_fields=unrepairable,
            originating_observation_id=originating_observation_id,
        )

    def apply(
        self,
        plan: ProviderRepairPlan,
        context: AdapterContext,
        *,
        initiated_by: str = "system",
        simulate_outcome_unknown: bool = False,
    ) -> ProviderRepairResult:
        stored = self._store.get(plan.object_id)
        if stored is None:
            return self._fail(plan, RepairStatus.PROVIDER_NOT_FOUND, "OBJECT_NOT_FOUND")

        if stored.version != plan.canonical_expected_version:
            return self._conflict(
                plan,
                RepairStatus.CANONICAL_CONFLICT,
                stored.version,
                None,
                initiated_by=initiated_by,
            )

        if not self._changes_match_canonical(stored, plan.changes):
            return self._fail(plan, RepairStatus.REJECTED_TAMPER, "REPAIR_VALUES_NOT_CANONICAL")

        binding = ProviderBinding.from_metadata(stored.metadata)
        if binding is None:
            return self._fail(plan, RepairStatus.PROVIDER_NOT_FOUND, "NO_PROVIDER_BINDING")
        if (binding.provider_name != plan.provider_name
                or (binding.external_id or stored.id) != plan.external_id
                or stored.type != plan.type_identity or stored.type_version != plan.type_version):
            return self._fail(plan, RepairStatus.REJECTED_TAMPER, "REPAIR_BINDING_MISMATCH")

        try:
            projection, token = self._resolve_projection(stored, binding.provider_name, operation="patch")
            if token != plan.projection_token:
                raise ProviderProjectionUnsupported("PROVIDER_CONFIGURATION_CHANGED")
            caps = projection.capabilities
            if any(not caps.can_write(field) or field not in caps.representable_fields
                   for field in plan.changes):
                raise ProviderProjectionUnsupported("UNSUPPORTED_PROVIDER_FIELD")
            if not plan.provider_expected_version:
                raise ProviderProjectionUnsupported("PROVIDER_PRECONDITION_UNAVAILABLE")
            provider = self._providers.get(plan.provider_name)
            current = provider.fetch(plan.external_id)
            actual_provider_version = projection.version(current)
        except ProviderProjectionUnsupported as exc:
            return self._fail(plan, RepairStatus.UNSUPPORTED_PROVIDER, str(exc))
        except KeyError:
            return self._fail(plan, RepairStatus.PROVIDER_NOT_FOUND, "PROVIDER_NOT_CONFIGURED")
        except ProviderObjectNotFound:
            return self._fail(plan, RepairStatus.PROVIDER_NOT_FOUND, "PROVIDER_NOT_FOUND")
        except ProviderError:
            return self._fail(plan, RepairStatus.PROVIDER_UNAVAILABLE, "PROVIDER_UNAVAILABLE")

        if actual_provider_version != str(plan.provider_expected_version):
            return self._conflict(
                plan,
                RepairStatus.PROVIDER_CONFLICT,
                stored.version,
                actual_provider_version,
                initiated_by=initiated_by,
            )

        try:
            already_in_sync = self._already_in_sync(current, plan.changes, projection)
        except ProviderProjectionUnsupported as exc:
            return self._fail(plan, RepairStatus.UNSUPPORTED_PROVIDER, str(exc))

        # Re-resolve after provider/converter calls, before any requested/write effect.
        live = self._store.get(plan.object_id)
        if live is None or live.version != plan.canonical_expected_version:
            return self._fail(plan, RepairStatus.CANONICAL_CONFLICT, "CANONICAL_CONFLICT")
        if ProviderBinding.from_metadata(live.metadata) != binding:
            return self._fail(plan, RepairStatus.REJECTED_TAMPER, "REPAIR_BINDING_MISMATCH")
        if not self._changes_match_canonical(live, plan.changes):
            return self._fail(plan, RepairStatus.REJECTED_TAMPER, "REPAIR_VALUES_NOT_CANONICAL")
        try:
            _, live_token = self._resolve_projection(live, binding.provider_name, operation="patch")
            if live_token != token or self._providers.get(plan.provider_name) is not provider:
                raise ProviderProjectionUnsupported("PROVIDER_CONFIGURATION_CHANGED")
        except ProviderProjectionUnsupported as exc:
            return self._fail(plan, RepairStatus.UNSUPPORTED_PROVIDER, str(exc))

        if already_in_sync:
            result = ProviderRepairResult(
                object_id=plan.object_id,
                repair_id=plan.plan_id,
                status=RepairStatus.ALREADY_IN_SYNC,
                reason="ALREADY_IN_SYNC",
                canonical_expected_version=plan.canonical_expected_version,
                canonical_actual_version=stored.version,
                provider_expected_version=plan.provider_expected_version,
                provider_actual_version=actual_provider_version,
                provider_name=plan.provider_name,
                changes=plan.changes,
                correlation_id=plan.plan_id,
            )
            self._repairs.save_repair(result, initiated_by=initiated_by)
            return result

        self._repairs.append_sync_event(
            object_id=plan.object_id,
            event_type="provider_repair.requested",
            repair_id=plan.plan_id,
            details={"changes": plan.changes},
        )

        if simulate_outcome_unknown:
            result = ProviderRepairResult(
                object_id=plan.object_id,
                repair_id=plan.plan_id,
                status=RepairStatus.PROVIDER_OUTCOME_UNKNOWN,
                reason="SIMULATED_TIMEOUT",
                canonical_expected_version=plan.canonical_expected_version,
                canonical_actual_version=stored.version,
                provider_expected_version=plan.provider_expected_version,
                provider_actual_version=actual_provider_version,
                provider_name=plan.provider_name,
                changes=plan.changes,
                correlation_id=plan.plan_id,
            )
            self._repairs.save_repair(result, initiated_by=initiated_by)
            return result

        try:
            updated = provider.apply_patch(
                plan.external_id,
                plan.changes,
                expected_version=plan.provider_expected_version,
                repair_id=plan.plan_id,
                idempotency_key=f"repair:{plan.plan_id}",
            )
        except ProviderConflict:
            return self._conflict(
                plan,
                RepairStatus.PROVIDER_CONFLICT,
                stored.version,
                actual_provider_version,
                initiated_by=initiated_by,
            )
        except ProviderOutcomeUnknown as exc:
            result = ProviderRepairResult(
                object_id=plan.object_id,
                repair_id=plan.plan_id,
                status=RepairStatus.PROVIDER_OUTCOME_UNKNOWN,
                reason=str(exc),
                canonical_expected_version=plan.canonical_expected_version,
                canonical_actual_version=stored.version,
                provider_expected_version=plan.provider_expected_version,
                provider_actual_version=actual_provider_version,
                provider_name=plan.provider_name,
                changes=plan.changes,
                correlation_id=plan.plan_id,
            )
            self._repairs.save_repair(result, initiated_by=initiated_by)
            return result
        except ProviderError as exc:
            return self._fail(plan, RepairStatus.PROVIDER_UNAVAILABLE, exc.code)

        try:
            new_provider_version = projection.version(updated)
        except ProviderProjectionUnsupported:
            return self._fail(plan, RepairStatus.PROVIDER_OUTCOME_UNKNOWN, "PROVIDER_RESPONSE_PRECONDITION_UNAVAILABLE")
        self._record_repair_echo(stored, plan.plan_id, new_provider_version)

        result = ProviderRepairResult(
            object_id=plan.object_id,
            repair_id=plan.plan_id,
            status=RepairStatus.APPLIED,
            reason="APPLIED",
            canonical_expected_version=plan.canonical_expected_version,
            canonical_actual_version=stored.version,
            provider_expected_version=plan.provider_expected_version,
            provider_actual_version=new_provider_version,
            provider_name=plan.provider_name,
            changes=plan.changes,
            correlation_id=plan.plan_id,
        )
        self._repairs.save_repair(
            result,
            initiated_by=initiated_by,
            originating_observation_id=plan.originating_observation_id,
        )
        self._repairs.append_sync_event(
            object_id=plan.object_id,
            event_type="provider_repair.applied",
            repair_id=plan.plan_id,
            details={"provider_version": new_provider_version},
        )
        return result

    def replace_provider(
        self,
        object_id: str,
        *,
        new_provider_name: str,
        new_external_id: str,
        context: AdapterContext,
    ) -> dict[str, Any]:
        stored = self._store.get(object_id)
        if stored is None:
            raise KeyError(f"object not found: {object_id}")

        old_binding = ProviderBinding.from_metadata(stored.metadata)
        projection, token = self._resolve_projection(stored, new_provider_name, operation="create")
        provider = self._providers.get(new_provider_name)
        caps = projection.capabilities
        projection_gaps: list[str] = []
        canonical_fields = frozenset(stored.data.keys()) | {"state"}
        projection_gaps = sorted(caps.projection_gaps(canonical_fields))
        if self._resolve_projection(stored, new_provider_name, operation="create")[1] != token:
            raise ProviderProjectionUnsupported("PROVIDER_CONFIGURATION_CHANGED")
        provider.create_from_canonical(new_external_id, dict(stored.data), stored.state)

        live = self._store.get(object_id)
        if live is None:
            raise ProviderOutcomeUnknown("CANONICAL_OBJECT_UNAVAILABLE_AFTER_PROVIDER_CREATE")
        if ProviderBinding.from_metadata(live.metadata) != old_binding:
            raise ProviderOutcomeUnknown("PROVIDER_BINDING_CHANGED_DURING_CREATE")
        metadata = dict(live.metadata)
        if old_binding:
            metadata["detached_from"] = old_binding.model_dump(exclude_none=True)
        metadata["provider_binding"] = ProviderBinding(
            provider_type=projection.provider_type,
            provider_name=new_provider_name,
            external_id=new_external_id,
        ).to_metadata()["provider_binding"]
        if projection_gaps:
            metadata["provider_projection_gaps"] = projection_gaps

        updated = live.model_copy(update={"metadata": metadata})
        self._store.save(updated)

        self._repairs.append_sync_event(
            object_id=object_id,
            event_type="provider_replacement.completed",
            details={
                "new_provider": new_provider_name,
                "new_external_id": new_external_id,
                "projection_gaps": projection_gaps,
            },
        )
        return {
            "object_id": object_id,
            "new_provider": new_provider_name,
            "new_external_id": new_external_id,
            "projection_gaps": projection_gaps,
        }

    def _load_views(
        self, object_id: str, context: AdapterContext
    ) -> tuple:
        stored = self._store.get(object_id)
        if stored is None:
            return None, None, None, None, None, None
        binding = ProviderBinding.from_metadata(stored.metadata)
        if binding is None or binding.is_local:
            return stored, None, None, None, None, None
        projection, token = self._resolve_projection(stored, binding.provider_name, operation="patch")
        try:
            provider = self._providers.get(binding.provider_name)
            record = provider.fetch(binding.external_id or stored.id)
            version = projection.version(record)
            data, state = projection.normalize(record)
        except ProviderProjectionUnsupported:
            raise
        except ProviderError:
            return stored, None, binding, projection, token, None
        view = stored.model_copy(update={"data": data, "state": state})
        return stored, view, binding, projection, token, version

    def _resolve_projection(
        self, stored: GovernedObject, provider_name: str, *, operation: str,
    ) -> tuple[ProviderProjection, str]:
        if not self._types.supports_type_version(stored.type, stored.type_version):
            raise ProviderProjectionUnsupported("BOUND_TYPE_VERSION_UNSUPPORTED")
        projection, token = self._providers.resolve_projection(provider_name, stored.type, stored.type_version)
        caps = projection.capabilities
        if caps is None:
            raise ProviderProjectionUnsupported("PROVIDER_CAPABILITIES_NOT_CONFIGURED")
        try:
            provider = self._providers.get(provider_name)
        except KeyError as exc:
            raise ProviderProjectionUnsupported("PROVIDER_NOT_CONFIGURED") from exc
        if operation == "patch":
            if not caps.supports_conditional_write or not callable(getattr(provider, "apply_patch", None)):
                raise ProviderProjectionUnsupported("CONDITIONAL_PROVIDER_WRITE_UNSUPPORTED")
        elif not caps.supports_create_from_canonical or not callable(getattr(provider, "create_from_canonical", None)):
            raise ProviderProjectionUnsupported("PROVIDER_CREATE_UNSUPPORTED")
        return projection, token

    def _drift_fields(self, stored: GovernedObject, provider_view: GovernedObject) -> list[str]:
        fields: list[str] = []
        if stored.state != provider_view.state:
            fields.append("state")
        for key in set(stored.data.keys()) | set(provider_view.data.keys()):
            if stored.data.get(key) != provider_view.data.get(key):
                fields.append(key)
        return fields

    def _changes_match_canonical(self, stored: GovernedObject, changes: dict[str, Any]) -> bool:
        for field, value in changes.items():
            canonical_val = stored.state if field == "state" else stored.data.get(field)
            if canonical_val != value:
                return False
        return True

    def _already_in_sync(
        self,
        provider_record: dict[str, Any],
        changes: dict[str, Any],
        projection: ProviderProjection,
    ) -> bool:
        data, state = projection.normalize(provider_record)
        for field, expected in changes.items():
            actual = state if field == "state" else data.get(field)
            if actual != expected:
                return False
        return True

    def _record_repair_echo(
        self, stored: GovernedObject, repair_id: str, provider_version: str
    ) -> None:
        current = self._store.get(stored.id)
        if current is None or ProviderBinding.from_metadata(current.metadata) != ProviderBinding.from_metadata(stored.metadata):
            return
        stored = current
        meta = dict(stored.metadata)
        meta["_provider_repair_echo"] = {
            "repair_id": repair_id,
            "provider_version": provider_version,
        }
        sync = dict(meta.get("_sync_state") or {})
        sync["last_applied_provider_version"] = provider_version
        sync["last_seen_provider_version"] = provider_version
        meta["_sync_state"] = sync
        self._store.save(stored.model_copy(update={"metadata": meta}))

    def _conflict(
        self,
        plan: ProviderRepairPlan,
        status: RepairStatus,
        canonical_actual: int,
        provider_actual: str | None,
        *,
        initiated_by: str,
    ) -> ProviderRepairResult:
        result = ProviderRepairResult(
            object_id=plan.object_id,
            repair_id=plan.plan_id,
            status=status,
            reason=status.value,
            canonical_expected_version=plan.canonical_expected_version,
            canonical_actual_version=canonical_actual,
            provider_expected_version=plan.provider_expected_version,
            provider_actual_version=provider_actual,
            provider_name=plan.provider_name,
            changes=plan.changes,
            correlation_id=plan.plan_id,
        )
        self._repairs.save_repair(result, initiated_by=initiated_by)
        self._repairs.append_sync_event(
            object_id=plan.object_id,
            event_type="provider_repair.conflict",
            repair_id=plan.plan_id,
            details={"status": status.value},
        )
        return result

    def _fail(
        self, plan: ProviderRepairPlan, status: RepairStatus, reason: str
    ) -> ProviderRepairResult:
        result = ProviderRepairResult(
            object_id=plan.object_id,
            repair_id=plan.plan_id,
            status=status,
            reason=reason,
            provider_name=plan.provider_name,
            changes=plan.changes,
        )
        self._repairs.save_repair(result)
        self._repairs.append_sync_event(
            object_id=plan.object_id,
            event_type="provider_repair.failed",
            repair_id=plan.plan_id,
            details={"reason": reason},
        )
        return result
