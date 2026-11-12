# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Brafford Labs, LLC
"""Independent exact-bound projections and no-effect refusal tests."""
from copy import deepcopy
from dataclasses import replace

import pytest

from governed_objects.domain.command import ReadObject, TransitionState
from governed_objects.domain.models import Actor, ActorType, Decision
from governed_objects.observations.models import ExternalObservation, FieldChange
from governed_objects.outbound.models import RepairStatus
from governed_objects.providers.binding import ProviderBinding
from governed_objects.providers.capabilities import ProviderCapabilities
from governed_objects.providers.errors import ProviderProjectionUnsupported, ProviderUnavailable
from governed_objects.providers.projection import ProviderProjection
from governed_objects.providers.registry import ProviderRegistry
from governed_objects.types.schema import ObjectTypeDefinition
from tests.fixtures.providers import KIND, VERSION, RawProvider, build_scenario, normalize, projection, revision, snapshot


@pytest.fixture
def scenario(tmp_path):
    result = build_scenario(tmp_path / "cell.db")
    yield result
    result.store.close()


def no_write(scenario, before, records):
    assert snapshot(scenario) == before
    assert scenario.provider.records == records and scenario.provider.writes == []
    events = scenario.store._conn.execute("SELECT event_type FROM sync_audit WHERE object_id = ?", ("cell",)).fetchall()
    assert not any(event["event_type"] in ("provider_repair.requested", "provider_repair.applied") for event in events)


def reconfigure(scenario, descriptor):
    scenario.providers.remove_projection(scenario.provider.name, KIND, VERSION)
    scenario.providers.register_projection(scenario.provider.name, KIND, VERSION, descriptor)


@pytest.mark.parametrize("name", ["unrelated-b", "completely-different"])
def test_nonbusiness_exact_projection_and_repair(tmp_path, name):
    scenario = build_scenario(tmp_path / "cell.db", provider_name=name)
    try:
        before = snapshot(scenario)
        plan = scenario.runtime.plan_repair("cell")
        assert plan.changes == {"value": "accepted"}
        assert plan.provider_expected_version == "opaque:rev/drift"
        assert plan.type_identity == KIND and plan.type_version == VERSION
        result = scenario.runtime.apply_repair(plan)
        assert result.status == RepairStatus.APPLIED
        assert scenario.provider.records["wire-id"]["signal"] == "accepted"
        assert len(scenario.provider.writes) == 1
        after = snapshot(scenario)
        assert {k: v for k, v in after.items() if k != "object"} == {k: v for k, v in before.items() if k != "object"}
        assert after["object"] | {"metadata": before["object"]["metadata"]} == before["object"]
        scenario.runtime.apply_repair(plan)
        assert len(scenario.provider.writes) == 1
    finally:
        scenario.store.close()


def test_defaults_conservative_and_bootstrap_not_create():
    caps = ProviderCapabilities("independent")
    assert not any((caps.supports_conditional_write, caps.supports_webhooks, caps.supports_bootstrap,
                    caps.supports_create_from_canonical))
    assert not caps.can_write("value") and caps.projection_gaps(frozenset({"value"})) == {"value"}


def test_exact_registry_keys_no_type_or_version_inference():
    registry = ProviderRegistry()
    provider = RawProvider()
    registry.register(provider)
    first = projection()
    second = ProviderProjection(lambda record: ({"other": record["signal"]}, "different"), revision,
                                ProviderCapabilities(provider.name))
    registry.register_projection(provider.name, KIND, VERSION, first)
    registry.register_projection(provider.name, KIND, "another:opaque", second)
    registry.register_projection(provider.name, "different.type", VERSION, second)
    assert registry.resolve_projection(provider.name, KIND, VERSION)[0] is first
    assert registry.resolve_projection(provider.name, KIND, "another:opaque")[0] is second
    for key in [("renamed-b", KIND, VERSION), (provider.name, KIND, "1"), (provider.name, "missing", VERSION)]:
        with pytest.raises(ProviderProjectionUnsupported):
            registry.resolve_projection(*key)
    with pytest.raises(ValueError, match="conflicting"):
        registry.register_projection(provider.name, KIND, VERSION, second)
    with pytest.raises(ValueError, match="mismatch"):
        registry.register_projection(provider.name, KIND, "unused", projection("wrong"))
    registry.register_projection(provider.name, KIND, VERSION, first)
    registry.register(RawProvider())
    with pytest.raises(ProviderProjectionUnsupported):
        registry.resolve_projection(provider.name, KIND, VERSION)


@pytest.mark.parametrize("failure", ["missing", "caps", "conditional", "patch", "token-none", "token-empty", "token-int",
                                     "unwritable", "unrepresentable", "unsupported-version", "missing-kind"])
def test_unsupported_plan_has_no_effect(scenario, failure):
    if failure == "missing":
        scenario.providers.remove_projection(scenario.provider.name, KIND, VERSION)
    elif failure == "caps":
        reconfigure(scenario, replace(scenario.descriptor, capabilities=None))
    elif failure == "conditional":
        reconfigure(scenario, projection(supports_conditional_write=False))
    elif failure == "patch":
        scenario.provider.apply_patch = None
    elif failure.startswith("token-"):
        scenario.provider.records["wire-id"]["revision"] = {"token-none": None, "token-empty": "", "token-int": 7}[failure]
    elif failure == "unwritable":
        reconfigure(scenario, projection(writable_fields=frozenset()))
    elif failure == "unrepresentable":
        reconfigure(scenario, projection(representable_fields=frozenset()))
    elif failure == "unsupported-version":
        scenario.types.register(scenario.types.get(KIND).model_copy(update={"type_version": "replacement"}))
    else:
        scenario.types._types.pop(KIND)
    before, records = snapshot(scenario), deepcopy(scenario.provider.records)
    with pytest.raises(ProviderProjectionUnsupported):
        scenario.runtime.plan_repair("cell")
    no_write(scenario, before, records)


@pytest.mark.parametrize("failure", ["removed", "changed", "no-caps", "conditional", "patch", "wrong-provider", "wrong-id",
                                     "wrong-type", "wrong-version", "wrong-revision", "empty-revision", "tampered-value",
                                     "noncanonical-field", "empty-changes", "unsupported-version"])
def test_stale_or_tampered_plan_refuses_without_effect(scenario, failure):
    plan = scenario.runtime.plan_repair("cell")
    expected = RepairStatus.UNSUPPORTED_PROVIDER
    if failure == "removed":
        scenario.providers.remove_projection(scenario.provider.name, KIND, VERSION)
    elif failure == "changed":
        reconfigure(scenario, scenario.descriptor)
    elif failure == "no-caps":
        reconfigure(scenario, replace(scenario.descriptor, capabilities=None))
    elif failure == "conditional":
        reconfigure(scenario, projection(supports_conditional_write=False))
    elif failure == "patch":
        scenario.provider.apply_patch = None
    elif failure.startswith("wrong-") and failure != "wrong-revision":
        field = {"wrong-provider": "provider_name", "wrong-id": "external_id", "wrong-type": "type_identity",
                 "wrong-version": "type_version"}[failure]
        setattr(plan, field, "wrong")
        expected = RepairStatus.REJECTED_TAMPER
    elif failure == "wrong-revision":
        plan.provider_expected_version = "not-the-token"
        expected = RepairStatus.PROVIDER_CONFLICT
    elif failure == "empty-revision":
        plan.provider_expected_version = ""
    elif failure == "tampered-value":
        plan.changes["value"] = "invented"
        expected = RepairStatus.REJECTED_TAMPER
    elif failure == "noncanonical-field":
        plan.changes["not-declared"] = None
    elif failure == "empty-changes":
        plan.changes.clear()
        scenario.providers.remove_projection(scenario.provider.name, KIND, VERSION)
    elif failure == "unsupported-version":
        scenario.types.register(scenario.types.get(KIND).model_copy(update={"type_version": "replacement"}))
    before, records = snapshot(scenario), deepcopy(scenario.provider.records)
    result = scenario.runtime.apply_repair(plan)
    assert result.status == expected
    no_write(scenario, before, records)


@pytest.mark.parametrize("change", ["projection", "binding", "canonical"])
def test_recheck_after_provider_fetch_before_write(scenario, change):
    plan = scenario.runtime.plan_repair("cell")
    before = snapshot(scenario)
    def hook():
        if change == "projection":
            scenario.providers.remove_projection(scenario.provider.name, KIND, VERSION)
        elif change == "binding":
            obj = scenario.store.get("cell")
            metadata = dict(obj.metadata)
            metadata["provider_binding"] = ProviderBinding(provider_name="other", external_id="other").model_dump()
            scenario.store.save(obj.model_copy(update={"metadata": metadata}))
        else:
            scenario.types.register(scenario.types.get(KIND).model_copy(update={"type_version": "replacement"}))
    scenario.provider.fetch_hook = hook
    result = scenario.runtime.apply_repair(plan)
    assert result.status in (RepairStatus.UNSUPPORTED_PROVIDER, RepairStatus.REJECTED_TAMPER)
    assert scenario.provider.writes == []
    after = snapshot(scenario)
    assert after["history"] == before["history"] and after["high_water"] == before["high_water"]
    assert after["object"]["data"] == before["object"]["data"]


@pytest.mark.parametrize("failure", ["missing", "bootstrap-only", "method", "caps", "version"])
def test_unsupported_replacement_does_not_create_or_rebind(scenario, failure):
    target = RawProvider("new-target")
    scenario.providers.register(target)
    descriptor = projection(target.name)
    if failure == "bootstrap-only":
        descriptor = replace(descriptor, capabilities=replace(descriptor.capabilities, supports_create_from_canonical=False))
    elif failure == "method":
        target.create_from_canonical = None
    elif failure == "caps":
        descriptor = replace(descriptor, capabilities=None)
    elif failure == "version":
        scenario.types.register(scenario.types.get(KIND).model_copy(update={"type_version": "replacement"}))
    if failure != "missing":
        scenario.providers.register_projection(target.name, KIND, VERSION, descriptor)
    before, records = snapshot(scenario), deepcopy(scenario.provider.records)
    with pytest.raises(ProviderProjectionUnsupported):
        scenario.runtime.replace_provider("cell", new_provider_name=target.name, new_external_id="new-wire")
    no_write(scenario, before, records)
    assert target.writes == [] and "new-wire" not in target.records


def test_replacement_preserves_canonical_and_explicit_label(scenario):
    target = RawProvider("target-b")
    scenario.providers.register(target)
    scenario.providers.register_projection(target.name, KIND, VERSION, projection(target.name))
    before = snapshot(scenario)
    scenario.runtime.replace_provider("cell", new_provider_name=target.name, new_external_id="new-wire")
    after = snapshot(scenario)
    assert after["object"]["metadata"]["provider_binding"]["provider_type"] == "test-cell"
    assert after["object"]["metadata"]["detached_from"] == before["object"]["metadata"]["provider_binding"]
    assert after["object"] | {"metadata": before["object"]["metadata"]} == before["object"]
    assert after["history"] == before["history"] and after["relationships"] == before["relationships"]
    assert target.records["new-wire"]["signal"] == "accepted"


def test_projection_metadata_does_not_authorize_canonical_mutation(scenario):
    outsider = Actor(id="system", type=ActorType.SERVICE, capabilities=[])
    before = snapshot(scenario)
    for command in (ReadObject("cell"), TransitionState("cell", "close")):
        result = scenario.runtime.execute(command, actor=outsider)
        assert result.decision == Decision.DENY and result.outcome_category == "AUTHORIZATION_DENIED"
        assert snapshot(scenario) == before
    result = scenario.runtime.ingest_observation(ExternalObservation(
        object_id="cell", provider_name=scenario.provider.name, external_id="wire-id", provider_type="system",
        provider_event_id="independent-authority-negative",
        changes={"value": FieldChange(previous_observed="accepted", observed="drift")},
    ))
    assert result.field_results["value"] == "REJECT"
    assert snapshot(scenario) == before and scenario.provider.writes == []


def test_unknown_outcome_and_provider_conflict(scenario):
    plan = scenario.runtime.plan_repair("cell")
    before = snapshot(scenario)
    scenario.provider.unknown = True
    result = scenario.runtime.apply_repair(plan)
    assert result.status == RepairStatus.PROVIDER_OUTCOME_UNKNOWN and len(scenario.provider.writes) == 1
    assert snapshot(scenario) == before
    scenario.runtime.apply_repair(plan)
    assert len(scenario.provider.writes) == 1 and snapshot(scenario) == before


def test_registration_requires_actual_type_and_state_before_effects(scenario):
    service = scenario.runtime.registration
    before = snapshot(scenario)
    with pytest.raises(KeyError):
        service.register_with_binding("absent", "unknown", ProviderBinding(), context=scenario.runtime.context)
    scenario.types.register(ObjectTypeDefinition(kind="empty", type_version="opaque:empty"))
    with pytest.raises(ValueError, match="default state"):
        service.register_with_binding("absent", "empty", ProviderBinding(), context=scenario.runtime.context)
    with pytest.raises(ValueError, match="not declared"):
        service.register_with_binding("absent", KIND, ProviderBinding(), state="invented", context=scenario.runtime.context)
    assert scenario.store.get("absent") is None and scenario.runtime.accepted_history("absent") == []
    assert snapshot(scenario) == before


def test_legacy_labels_preserved_as_data_and_neutral_new_defaults():
    observation = ExternalObservation(object_id="x", provider_name="p", external_id="e")
    assert observation.provider_type == "generic"
    binding = ProviderBinding.from_metadata({"provider_binding": {"provider": "legacy-b", "provider_id": "old",
                                                                  "provider_type": "reservation"}})
    assert binding.provider_type == "reservation" and binding.provider_name == "legacy-b" and binding.external_id == "old"
    assert ProviderBinding.from_metadata({"provider_binding": {"provider": "legacy-b"}}).provider_type == "generic"


def test_canonical_precondition_is_independent_and_uses_real_acceptance(scenario):
    plan = scenario.runtime.plan_repair("cell")
    writer = Actor(id="writer", type=ActorType.SERVICE, capabilities=["cell.close", "cell.read"])
    accepted = scenario.runtime.execute(TransitionState("cell", "close"), actor=writer)
    assert accepted.decision == Decision.ALLOW
    before, records = snapshot(scenario), deepcopy(scenario.provider.records)
    assert scenario.runtime.apply_repair(plan).status == RepairStatus.CANONICAL_CONFLICT
    no_write(scenario, before, records)


@pytest.mark.parametrize("failure", ["unavailable", "not-found", "absent-provider"])
def test_provider_failure_is_not_sync_or_mutation(scenario, failure):
    plan = scenario.runtime.plan_repair("cell")
    if failure == "unavailable":
        def unavailable(_):
            raise ProviderUnavailable("offline test failure")
        scenario.provider.fetch = unavailable
        expected = RepairStatus.PROVIDER_UNAVAILABLE
    elif failure == "not-found":
        scenario.provider.records.clear()
        expected = RepairStatus.PROVIDER_NOT_FOUND
    else:
        scenario.providers._providers.pop(scenario.provider.name)
        expected = RepairStatus.UNSUPPORTED_PROVIDER
    before, records = snapshot(scenario), deepcopy(scenario.provider.records)
    assert scenario.runtime.apply_repair(plan).status == expected
    no_write(scenario, before, records)


def test_missing_normalization_or_precondition_extractor_cannot_register():
    registry = ProviderRegistry()
    provider = RawProvider()
    registry.register(provider)
    for descriptor in (ProviderProjection(None, revision), ProviderProjection(normalize, None)):
        with pytest.raises(ValueError, match="explicit normalization"):
            registry.register_projection(provider.name, KIND, VERSION, descriptor)


@pytest.mark.parametrize("remove_at_apply", [False, True])
def test_review_keep_canonical_reports_unsupported_side_repair(scenario, remove_at_apply, monkeypatch):
    definition = scenario.types.get(KIND)
    scenario.types.register(definition.model_copy(update={"extra": {"authority": {"fields": {"value": "REQUIRES_REVIEW"}}}}))
    review = scenario.runtime.ingest_observation(ExternalObservation(
        object_id="cell", provider_name=scenario.provider.name, external_id="wire-id", provider_event_id="review-test",
        changes={"value": FieldChange(previous_observed="accepted", observed="drift")},
    ))
    assert review.decision == "REQUIRES_REVIEW" and review.review_case_id
    if remove_at_apply:
        original = scenario.runtime.outbound.apply
        def apply(plan, *args, **kwargs):
            scenario.providers.remove_projection(scenario.provider.name, KIND, VERSION)
            return original(plan, *args, **kwargs)
        monkeypatch.setattr(scenario.runtime.outbound, "apply", apply)
    else:
        scenario.providers.remove_projection(scenario.provider.name, KIND, VERSION)
    before, records = snapshot(scenario), deepcopy(scenario.provider.records)
    result = scenario.runtime.resolve_review(review.review_case_id, resolution="KEEP_CANONICAL", resolved_by="trusted-review-admin")
    assert result.decision == "REJECT" and result.reason == "REVIEW_KEEP_CANONICAL"
    assert result.field_results["value"] == "UNSUPPORTED_PROVIDER"
    no_write(scenario, before, records)


def test_synchronization_metadata_never_overwrites_independent_acceptance(scenario, monkeypatch):
    plan = scenario.runtime.plan_repair("cell")
    original = scenario.provider.apply_patch
    writer = Actor(id="writer", type=ActorType.SERVICE, capabilities=["cell.close", "cell.read"])
    accepted = []
    def write(*args, **kwargs):
        result = original(*args, **kwargs)
        outcome = scenario.runtime.execute(TransitionState("cell", "close"), actor=writer)
        assert outcome.decision == Decision.ALLOW
        accepted.append(snapshot(scenario))
        return result
    monkeypatch.setattr(scenario.provider, "apply_patch", write)
    assert scenario.runtime.apply_repair(plan).status == RepairStatus.APPLIED
    after = snapshot(scenario)
    assert after["object"] | {"metadata": accepted[0]["object"]["metadata"]} == accepted[0]["object"]
    assert after["history"] == accepted[0]["history"] and after["high_water"] == accepted[0]["high_water"]


def test_registration_does_not_invent_type_version(scenario):
    definition = ObjectTypeDefinition(kind="undeclared-version", initial_state="open")
    scenario.types.register(definition)
    with pytest.raises(ValueError, match="Type Version"):
        scenario.runtime.registration.register_with_binding("absent", definition.kind, ProviderBinding(), context=scenario.runtime.context)
    assert scenario.store.get("absent") is None and scenario.runtime.accepted_history("absent") == []


def test_read_only_projection_is_valid_observation_not_repair(scenario):
    reconfigure(scenario, replace(scenario.descriptor, capabilities=None))
    before = snapshot(scenario)
    observed = scenario.runtime._materializer.observe_provider(scenario.store.get("cell"), scenario.runtime.context)
    assert observed.object.data == {"value": "drift"} and observed.object.state == "open"
    assert snapshot(scenario) == before
    with pytest.raises(ProviderProjectionUnsupported, match="CAPABILITIES_NOT_CONFIGURED"):
        scenario.runtime.plan_repair("cell")
    assert scenario.provider.writes == []


def test_changed_opaque_provider_precondition_conflicts(scenario):
    plan = scenario.runtime.plan_repair("cell")
    scenario.provider.records["wire-id"]["revision"] = "opaque:rev/unordered-another"
    before, records = snapshot(scenario), deepcopy(scenario.provider.records)
    assert scenario.runtime.apply_repair(plan).status == RepairStatus.PROVIDER_CONFLICT
    no_write(scenario, before, records)


def test_projection_gaps_explicit_and_canonical_meaning_retained(scenario):
    target = RawProvider("partial-target")
    scenario.providers.register(target)
    scenario.providers.register_projection(target.name, KIND, VERSION,
                                          projection(target.name, representable_fields=frozenset({"state"})))
    before = snapshot(scenario)
    result = scenario.runtime.replace_provider("cell", new_provider_name=target.name, new_external_id="partial")
    assert result["projection_gaps"] == ["value"]
    assert scenario.store.get("cell").data == before["object"]["data"]
    target.records["partial"]["signal"] = "drift"
    with pytest.raises(ProviderProjectionUnsupported, match="UNREPRESENTABLE"):
        scenario.runtime.plan_repair("cell")
    assert len(target.writes) == 1


def test_completed_admission_not_relabelled_as_atomic_failure(scenario):
    definition = scenario.types.get(KIND)
    scenario.types.register(definition.model_copy(update={"extra": {"authority": {"fields": {"value": "PROVIDER_AUTO_ACCEPT"}}}}))
    scenario.providers.remove_projection(scenario.provider.name, KIND, VERSION)
    before = snapshot(scenario)
    result = scenario.runtime.ingest_observation(ExternalObservation(
        object_id="cell", provider_name=scenario.provider.name, external_id="wire-id", provider_event_id="accepted-without-side-sync",
        changes={"value": FieldChange(previous_observed="accepted", observed="drift")},
    ))
    assert result.decision == "ACCEPT" and result.field_results["value"] == "ACCEPT"
    assert result.field_results["provider_sync"] == "UNSUPPORTED_PROVIDER"
    assert scenario.store.get("cell").version == before["object"]["version"] + 1
    assert len(scenario.runtime.accepted_history("cell")) == len(before["history"]) + 1
    assert scenario.provider.writes == []
