# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Brafford Labs, LLC
"""Observation admission: captured authority, pure validity, staged local effects."""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

from governed_objects.adapters.context import AdapterContext
from governed_objects.adapters.registry import AdapterRegistry
from governed_objects.authority.rules import AuthorityMode, AuthorityRuleResolver
from governed_objects.domain.models import ActorType, Decision, GovernedObject, GovernanceEvent
from governed_objects.events.replay import bound_type_definition
from governed_objects.observations.models import (
    UNKNOWN_EXTERNAL_ACTOR, ExternalObservation, FieldChange, ObservationDecision, ObservationResult,
)
from governed_objects.observations.store import ObservationStore
from governed_objects.observations.validation import (
    CandidateMode, CandidateVerdict, candidate_image, capture_admission, validate_candidate,
)
from governed_objects.outbound.models import RepairStatus
from governed_objects.persistence.sqlite import INT64_MAX, SQLiteStore
from governed_objects.providers.errors import ProviderError, ProviderProjectionUnsupported
from governed_objects.relationships.sync import RelationshipSyncService
from governed_objects.types.registry import TypeRegistry


class _PlanRefused(Exception):
    pass


class ObservationIngestionService:
    """Partial semantic outcomes, installed as one owned local admission plan."""

    def __init__(self, store: SQLiteStore, type_registry: TypeRegistry,
                 observation_store: ObservationStore | None = None,
                 rel_sync: RelationshipSyncService | None = None,
                 adapters: AdapterRegistry | None = None,
                 adapter_context: AdapterContext | None = None,
                 outbound: Any | None = None) -> None:
        self._store = store
        self._types = type_registry
        self._authority = AuthorityRuleResolver(type_registry)
        self._observations = observation_store if observation_store is not None else ObservationStore(store)
        self._rel_sync = rel_sync
        self._adapters = adapters
        self._adapter_context = adapter_context
        self._outbound = outbound

    @property
    def observation_store(self) -> ObservationStore:
        return self._observations

    @staticmethod
    def _refusal(observation_id: str, reason: str) -> ObservationResult:
        return ObservationResult(observation_id=observation_id, decision=ObservationDecision.REJECT, reason=reason)

    def ingest(self, observation: ExternalObservation) -> ObservationResult:
        obj = self._store.get(observation.object_id)
        if obj is None:
            return self._finalize(observation, self._refusal(observation.id, "OBJECT_NOT_FOUND"))
        if bound_type_definition(self._types, obj) is None:
            return self._refusal(observation.id, "TYPE_VERSION_UNSUPPORTED")
        observation = observation.model_copy(deep=True)
        if self._observations.exists_dedupe_key(observation.dedupe_key()):
            return ObservationResult(observation_id=observation.id, decision=ObservationDecision.NO_CHANGE,
                                     reason="DUPLICATE_OBSERVATION")
        if self._is_echo(obj, observation):
            return self._finalize(observation, ObservationResult(observation_id=observation.id,
                decision=ObservationDecision.ECHO, reason="GOVERNED_COMMAND_ECHO"))
        if self._is_stale(obj, observation):
            return self._finalize(observation, ObservationResult(observation_id=observation.id,
                decision=ObservationDecision.STALE, reason="STALE_PROVIDER_VERSION"))
        if not observation.changes and not observation.state_change:
            return self._finalize(observation, ObservationResult(observation_id=observation.id,
                decision=ObservationDecision.NO_CHANGE, reason="NO_DIFFERENCES"))
        # A legacy bootstrap marker is provenance, never an authority grant.
        return self._process_changes(obj, observation)

    def _baseline(self, obj):
        return (self._store.version_high_water(obj.id), self._store.list_accepted_for_object(obj.id))

    def _check_current(self, obj, baseline, snapshot, case=None):
        if not snapshot.matches(self._types, self._adapters, obj):
            raise _PlanRefused("VALIDATION_UNAVAILABLE")
        if self._store.get(obj.id) != obj or self._baseline(obj) != baseline:
            raise _PlanRefused("CANONICAL_CHANGED")
        if case is not None and self._observations.get_review_case(case["id"]) != case:
            raise _PlanRefused("CANONICAL_CHANGED")

    def _verdict(self, snapshot, current, candidate, mode):
        return validate_candidate(type_def=snapshot.definition, current=candidate_image(current),
                                  candidate=candidate_image(candidate), mode=mode, adapter=snapshot.adapter)

    def _process_changes(self, obj: GovernedObject, observation: ExternalObservation) -> ObservationResult:
        baseline = self._baseline(obj)
        try:
            snapshot = capture_admission(self._types, self._adapters, obj)
            rules = self._authority.rules_for_definitions(snapshot.definitions)
        except Exception:
            return self._refusal(observation.id, "VALIDATION_UNAVAILABLE")
        current = obj.model_copy(deep=True)
        steps = []
        pending = []
        fields = {}
        repairs = []
        reasons = []
        invalid = False
        dimensions = []
        if observation.state_change is not None:
            dimensions.append(("state", observation.state_change, True))
        dimensions.extend((name, change, False) for name, change in observation.changes.items())
        for name, change, is_state in dimensions:
            previous = current.state if is_state else current.data.get(name)
            if previous == change.observed:
                fields[name] = ObservationDecision.NO_CHANGE.value
                continue
            rule = rules.fields.get(name)
            mode = rule.mode if rule is not None else AuthorityMode.REQUIRES_REVIEW
            if mode == AuthorityMode.CANONICAL_AUTHORITATIVE:
                fields[name] = ObservationDecision.REJECT.value
                repairs.append(name)
                reasons.append("CANONICAL_AUTHORITATIVE")
                continue
            if mode == AuthorityMode.REQUIRES_REVIEW:
                pending.append(dict(case_id=str(uuid4()), object_id=obj.id, observation_id=observation.id,
                                    field_name=name, canonical_value=previous, observed_value=change.observed))
                fields[name] = ObservationDecision.REQUIRES_REVIEW.value
                reasons.append("REQUIRES_REVIEW")
                continue
            candidate = self._changed(current, name, change.observed, is_state=is_state)
            verdict = self._verdict(snapshot, current, candidate, CandidateMode.OBSERVATION)
            if verdict == CandidateVerdict.UNAVAILABLE:
                return self._refusal(observation.id, "VALIDATION_UNAVAILABLE")
            if verdict == CandidateVerdict.INVALID:
                fields[name] = ObservationDecision.REJECT.value
                reasons.append("CANDIDATE_INVALID")
                invalid = True
                continue
            steps.append((candidate, name, change, previous))
            current = candidate
            fields[name] = ObservationDecision.ACCEPT.value
            reasons.append("ACCEPTED")
        aggregate = (ObservationDecision.ACCEPT if steps else ObservationDecision.REQUIRES_REVIEW if pending
                     else ObservationDecision.REJECT if ObservationDecision.REJECT.value in fields.values()
                     else ObservationDecision.NO_CHANGE)
        result = ObservationResult(observation_id=observation.id, decision=aggregate,
            reason="MULTI_FIELD" if len(fields) > 1 else reasons[0] if reasons else "NO_APPLICABLE_CHANGES",
            field_results=fields, repair_candidates=repairs,
            review_case_id=pending[-1]["case_id"] if pending else None, object_version=obj.version)
        # Pending artifacts must be representable. Rejected non-JSON proposal
        # values need not be recorded/deduped as if they were canonical data.
        try:
            json.dumps(pending, allow_nan=False)
        except (TypeError, ValueError, OverflowError):
            return self._refusal(observation.id, "VALIDATION_UNAVAILABLE")
        record_observation = self._recordable(observation)
        try:
            self._check_current(obj, baseline, snapshot)
            with self._store.observation_transaction():
                self._check_current(obj, baseline, snapshot)
                top = max(obj.version, baseline[0] if baseline[0] is not None else obj.version)
                if len(steps) > INT64_MAX - top:
                    raise _PlanRefused("OBJECT_VERSION_SPACE_EXHAUSTED")
                previous_obj = obj
                for offset, (image, name, change, previous) in enumerate(steps, 1):
                    updated = image.model_copy(update={"version": top + offset,
                        "metadata": self._update_obs_meta(image, observation)})
                    event = self._event(previous_obj, updated, name, previous, change.observed,
                                        observation, "external_observation", None)
                    self._store.save(updated)
                    event = self._store.append_accepted(event)
                    result.canonical_event_id = event.event_id
                    result.object_version = updated.version
                    previous_obj = updated
                for case in pending:
                    self._observations.create_review_case(**case)
                if record_observation:
                    self._observations.save_observation(observation, decision=result.decision, reason=result.reason,
                                                        canonical_event_id=result.canonical_event_id)
        except _PlanRefused as exc:
            return self._refusal(observation.id, str(exc))
        if steps or (observation.source.value == "WEBHOOK" and not invalid):
            self._post_sync(observation.object_id, result)
        return result

    @staticmethod
    def _recordable(observation):
        try:
            json.dumps({"changes": {k: v.model_dump() for k, v in observation.changes.items()},
                        "state_change": observation.state_change.model_dump() if observation.state_change else None,
                        "raw_metadata": observation.raw_metadata}, allow_nan=False)
            return True
        except (TypeError, ValueError, OverflowError):
            return False

    @staticmethod
    def _changed(obj, field, value, *, is_state=None):
        if is_state is True or (is_state is None and field == "state"):
            return obj.model_copy(deep=True, update={"state": value})
        data = dict(obj.data)
        data[field] = value
        return obj.model_copy(deep=True, update={"data": data})

    def resolve_review(self, case_id: str, *, resolution: str, resolved_by: str) -> ObservationResult:
        case = self._observations.get_review_case(case_id)
        if case is None or case["state"] != "open":
            return self._refusal(case_id, "REVIEW_CASE_NOT_OPEN")
        obj = self._store.get(case["object_id"])
        if obj is None:
            return self._refusal(case_id, "OBJECT_NOT_FOUND")
        if bound_type_definition(self._types, obj) is None:
            return self._refusal(case_id, "TYPE_VERSION_UNSUPPORTED")
        if resolution not in ("ACCEPT_EXTERNAL", "KEEP_CANONICAL"):
            return self._refusal(case["observation_id"], "UNKNOWN_RESOLUTION")
        baseline = self._baseline(obj)
        try:
            snapshot = capture_admission(self._types, self._adapters, obj)
        except Exception:
            return self._refusal(case["observation_id"], "VALIDATION_UNAVAILABLE")
        field = case["field_name"]
        observed = json.loads(case["observed_value"])
        previous = obj.state if field == "state" else obj.data.get(field)
        candidate = self._changed(obj, field, observed)
        if resolution == "ACCEPT_EXTERNAL":
            verdict = self._verdict(snapshot, obj, candidate, CandidateMode.REVIEW_ACCEPT)
            if verdict != CandidateVerdict.VALID:
                return self._refusal(case["observation_id"], "CANDIDATE_INVALID" if verdict == CandidateVerdict.INVALID
                                     else "VALIDATION_UNAVAILABLE")
        changed = resolution == "ACCEPT_EXTERNAL" and previous != observed
        result = ObservationResult(observation_id=case["observation_id"],
            decision=ObservationDecision.ACCEPT if changed else ObservationDecision.NO_CHANGE if resolution == "ACCEPT_EXTERNAL"
            else ObservationDecision.REJECT,
            reason="ACCEPTED" if changed else "FIELD_UNCHANGED" if resolution == "ACCEPT_EXTERNAL" else "REVIEW_KEEP_CANONICAL",
            review_case_id=case_id, object_version=obj.version if resolution == "ACCEPT_EXTERNAL" else None)
        try:
            self._check_current(obj, baseline, snapshot, case)
            with self._store.observation_transaction():
                self._check_current(obj, baseline, snapshot, case)
                if changed:
                    top = max(obj.version, baseline[0] if baseline[0] is not None else obj.version)
                    if top >= INT64_MAX:
                        raise _PlanRefused("OBJECT_VERSION_SPACE_EXHAUSTED")
                    observation = ExternalObservation(id=case["observation_id"], object_id=obj.id,
                                                       provider_name="review", external_id="")
                    candidate = candidate.model_copy(update={"version": top + 1,
                        "metadata": self._update_obs_meta(candidate, observation)})
                    event = self._event(obj, candidate, field, previous, observed, observation,
                                        "reconciliation_review", resolved_by)
                    self._store.save(candidate)
                    event = self._store.append_accepted(event)
                    result.canonical_event_id = event.event_id
                    result.object_version = candidate.version
                self._observations.resolve_review_case(case_id, resolution=resolution, resolved_by=resolved_by,
                                                       canonical_event_id=result.canonical_event_id)
        except _PlanRefused as exc:
            return self._refusal(case["observation_id"], str(exc))
        if resolution == "KEEP_CANONICAL":
            self._repair_kept(obj, baseline, snapshot, field, resolved_by, case["observation_id"], result)
        return result

    def _repair_kept(self, obj, baseline, snapshot, field, resolved_by, observation_id, result):
        if self._outbound is None or self._adapter_context is None:
            return
        verdict = self._verdict(snapshot, obj, obj, CandidateMode.REPAIR_EXISTING)
        if verdict != CandidateVerdict.VALID:
            result.field_results[field] = ("CANDIDATE_INVALID" if verdict == CandidateVerdict.INVALID else "VALIDATION_UNAVAILABLE")
            return
        try:
            self._check_current(obj, baseline, snapshot)
        except _PlanRefused as exc:
            result.field_results[field] = str(exc)
            return
        try:
            plan = self._outbound.plan(obj.id, self._adapter_context, fields=[field],
                                       originating_observation_id=observation_id)
            if plan and plan.changes:
                repaired = self._outbound.apply(plan, self._adapter_context, initiated_by=resolved_by)
                result.repair_candidates = [field]
                if repaired.status == RepairStatus.UNSUPPORTED_PROVIDER:
                    result.field_results[field] = "UNSUPPORTED_PROVIDER"
        except ProviderProjectionUnsupported:
            result.field_results[field] = "UNSUPPORTED_PROVIDER"
        except ProviderError:
            result.field_results[field] = "PROVIDER_ERROR"

    @staticmethod
    def _event(previous, updated, field, old, new, observation, source, resolved_by):
        details = {"field": field, "previous": old, "new": new, "data": dict(updated.data),
            "source": source, "observation_id": observation.id, "provider_name": observation.provider_name,
            "provider_event_id": observation.provider_event_id, "provider_actor_id": observation.provider_actor_id,
            "actor_provenance": "KNOWN_EXTERNAL_ACTOR" if observation.provider_actor_id else UNKNOWN_EXTERNAL_ACTOR}
        if resolved_by:
            details["resolved_by"] = resolved_by
        return GovernanceEvent(object_id=updated.id, object_type=updated.type, type_version=updated.type_version,
            actor_id=observation.provider_actor_id or UNKNOWN_EXTERNAL_ACTOR, actor_type=ActorType.SERVICE.value,
            action="state_changed" if field == "state" else f"{field}_changed", decision=Decision.ALLOW,
            event_type="external_observation.accepted", previous_state=previous.state, new_state=updated.state,
            reason="external observation accepted", object_version=updated.version, details=details)

    def _finalize(self, observation, result, *, canonical_event_id=None):
        self._observations.save_observation(observation, decision=result.decision, reason=result.reason,
                                            canonical_event_id=canonical_event_id or result.canonical_event_id)
        if result.decision == ObservationDecision.ACCEPT or observation.source.value == "WEBHOOK":
            self._post_sync(observation.object_id, result)
        return result

    def _post_sync(self, object_id, result):
        try:
            self._sync_relationships(object_id)
        except ProviderProjectionUnsupported:
            result.field_results["provider_sync"] = "UNSUPPORTED_PROVIDER"
        except ProviderError:
            result.field_results["provider_sync"] = "PROVIDER_ERROR"

    def _sync_relationships(self, object_id: str) -> None:
        if self._rel_sync is None or self._adapters is None or self._adapter_context is None:
            return
        stored = self._store.get(object_id)
        if stored is None:
            return
        binding = stored.metadata.get("provider_binding") or {}
        provider_name = binding.get("provider_name") or binding.get("provider")
        if not provider_name:
            return
        adapter = self._adapters.resolve(stored)
        hydrated = adapter.load(stored, self._adapter_context)
        if hydrated and hydrated.relationships:
            self._rel_sync.sync_provider_relationships(object_id, hydrated.relationships, provider_name=provider_name)

    def _is_echo(self, obj: GovernedObject, observation: ExternalObservation) -> bool:
        repair_of = observation.raw_metadata.get("repair_of")
        if repair_of is None:
            repair_of = (observation.raw_metadata.get("webhook") or {}).get("repair_of")
        repair_echo = obj.metadata.get("_provider_repair_echo") or {}
        if repair_of and repair_echo.get("repair_id") == repair_of:
            return True
        echo = obj.metadata.get("_governed_echo") or {}
        if not echo:
            return False
        if observation.provider_event_id and echo.get("provider_event_id") == observation.provider_event_id:
            return True
        echo_of = observation.raw_metadata.get("echo_of")
        if echo_of is None:
            echo_of = (observation.raw_metadata.get("webhook") or {}).get("echo_of")
        return bool(echo_of and echo.get("correlation_id") == echo_of)

    def _is_stale(self, obj: GovernedObject, observation: ExternalObservation) -> bool:
        if observation.provider_version is None:
            return False
        last_version = (obj.metadata.get("_observation_state") or {}).get("last_accepted_provider_version")
        if last_version is None:
            return False
        try:
            return int(observation.provider_version) <= int(last_version)
        except (TypeError, ValueError):
            return observation.provider_version <= last_version

    def _update_obs_meta(self, obj, observation):
        meta = dict(obj.metadata)
        state = dict(meta.get("_observation_state") or {})
        if observation.provider_version is not None:
            state["last_accepted_provider_version"] = observation.provider_version
        meta["_observation_state"] = state
        return meta

    def record_governed_echo(self, obj: GovernedObject, *, correlation_id: str | None,
                            provider_event_id: str | None = None) -> GovernedObject:
        if not correlation_id and not provider_event_id:
            return obj
        meta = dict(obj.metadata)
        meta["_governed_echo"] = {"correlation_id": correlation_id, "provider_event_id": provider_event_id}
        return obj.model_copy(update={"metadata": meta})
