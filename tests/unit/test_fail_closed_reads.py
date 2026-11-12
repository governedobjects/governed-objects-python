# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Brafford Labs, LLC
"""Local fail-closed policy; generic real-command/policy/store tests."""
from types import SimpleNamespace

import pytest

from governed_objects.adapters.base import TypeAdapter
from governed_objects.adapters.mandate import MandateAdapter
from governed_objects.delegation.configuration import DelegationConfiguration
from governed_objects.delegation.materializer import reject_legacy_scope
from governed_objects.events.reducers.mandate import MandateEventReducer
from tests.fixtures.delegation import mandate_definition
from governed_objects.applications.registry import ApplicationDefinition, ApplicationRegistry
from governed_objects.domain.command import AddRelationship, CreateObject, ReadObject, TransitionState
from governed_objects.domain.command_context import CommandContext
from governed_objects.domain.models import Actor, ActorType, CommandResult, Decision, GovernedObject, Relationship, ValidationCheck
from governed_objects.persistence.sqlite import SQLiteStore
from governed_objects.providers.errors import ProviderUnavailable
from governed_objects.providers.registry import ProviderRegistry
from governed_objects.runtime.runtime import GovernedObjectRuntime
from governed_objects.types.extensions import TypeExtensionRegistry
from governed_objects.types.lifecycle import LifecycleValidator
from governed_objects.types.registry import TypeRegistry
from governed_objects.types.schema import ObjectTypeDefinition
from governed_objects.types.validation import SchemaValidator

KIND = "fail.closed.test/Record"
A, B = "opaque:a+bound", "opaque:z!replacement"
SECRET, PUBLIC = "test.secret.read", "test.public.read"


def definition(version=A, requirement=SECRET):
    return ObjectTypeDefinition(
        kind=KIND, type_version=version,
        schema={"public": {"type": "string", "required": True}, "secret": {"type": "string"}},
        initial_state="draft", states={"draft": {"transitions": {"finish": "done"}}, "done": {"terminal": True}},
        actions={"create": {"requires": ["test.write"]}, "finish": {"requires": ["test.write"]}},
        read_requires=[requirement],
    )


def actor(caps=()):
    return Actor(id="requester", type=ActorType.SERVICE, capabilities=list(caps))


class ProviderSpy:
    name = "test-read-provider"

    def __init__(self):
        self.calls = []
        self.block = False
        self.unavailable = False

    def read(self):
        self.calls.append("provider")
        if self.block:
            pytest.fail("provider reached on refused read")
        if self.unavailable:
            raise ProviderUnavailable("test provider unavailable")


class AdapterSpy(TypeAdapter):
    def __init__(self, provider):
        self.provider = provider
        self.calls = []
        self.block = False

    def supports(self, kind):
        return kind == KIND

    def validate_create(self, type_def, data):
        valid, checks, errors = SchemaValidator().validate(type_def, data)
        return valid, checks, "; ".join(errors)

    def validate_transition(self, obj, type_def, action, actor):
        valid, reason, check = LifecycleValidator().validate_transition(type_def, obj.state, action)
        return valid, [check], reason

    def load(self, obj, context):
        self.calls.append("hydrate")
        if self.block:
            pytest.fail("hydration reached on refused read")
        self.provider.read()
        return None

    def filter_read_data(self, obj, type_def, actor, fields):
        self.calls.append("filter")
        if self.block:
            pytest.fail("filter reached on refused read")
        fields = fields or list(obj.data)
        denied = [field for field in fields if field == "secret" and "test.secret.field" not in actor.capabilities]
        return {field: obj.data[field] for field in fields if field not in denied and field in obj.data}, denied


@pytest.fixture(params=["direct", "configured"])
def env(request, tmp_path):
    store = SQLiteStore(tmp_path / "reads.db")
    types = TypeRegistry()
    types.register(definition())
    provider = ProviderSpy()
    providers = ProviderRegistry()
    providers.register(provider)
    adapter = AdapterSpy(provider)
    applications = ApplicationRegistry()
    applications.register(ApplicationDefinition("full", "full", capabilities=frozenset({SECRET, PUBLIC})))
    applications.register(ApplicationDefinition("empty", "empty"))
    owner = actor(["test.write", SECRET, PUBLIC, "test.secret.field"])
    principal = owner.model_copy(update={'id': 'principal'})
    local_actors = {owner.id: owner, principal.id: principal}
    mandate_adapter = MandateAdapter()
    mandate_reducer = MandateEventReducer()
    mandate_type = mandate_definition()
    types.register(mandate_type)
    extensions = TypeExtensionRegistry()
    extensions.register_reducer('DelegatedMandate', mandate_reducer)
    delegation = DelegationConfiguration(mandate_type, mandate_adapter, mandate_reducer,
                                         reject_legacy_scope, local_actors.__getitem__)
    build = GovernedObjectRuntime if request.param == "direct" else GovernedObjectRuntime.configured
    rt = build(store=store, type_registry=types, adapters=[adapter, mandate_adapter], extension_registry=extensions,
               providers=providers, application_registry=applications, delegation=delegation)
    assert rt.execute(CreateObject("target", KIND, {"public": "visible", "secret": "protected"}), actor=owner).decision == Decision.ALLOW
    yield SimpleNamespace(rt=rt, store=store, types=types, provider=provider, adapter=adapter, owner=owner, entry=request.param)
    store.close()


def snapshot(env):
    return dict(object=env.store.get("target").model_dump(),
                high_water=env.store.version_high_water("target"),
                relationships=[r.model_dump() for r in env.rt.canonical_relationships("target")],
                history=[event.model_dump() for event in env.rt.accepted_history("target")])


def surface(result):
    value = result.model_dump(mode="json")
    assert result.decision == Decision.DENY and result.outcome_category == "AUTHORIZATION_DENIED"
    assert result.deny_reason == "READ_NOT_AUTHORIZED" and result.reason == "access denied"
    assert result.data == {} and result.events == [] and result.validation_checks == []
    assert all(value[name] is None for name in ["object_version", "actual_version", "expected_version", "previous_state", "new_state", "conflict_reason"])
    assert result.action == "read" and result.object_id == "target"
    return value


def forbid_effects(env, monkeypatch):
    env.adapter.calls.clear()
    env.provider.calls.clear()
    env.adapter.block = env.provider.block = True
    def no_save(*args, **kwargs):
        pytest.fail("write-through/canonical persistence on refused read")
    monkeypatch.setattr(env.store, "save", no_save)


def test_reported_counterexample_real_policy(env, monkeypatch):
    requester = actor([PUBLIC])
    initial = env.rt.execute(ReadObject("target"), actor=requester)
    assert initial.decision == Decision.DENY and initial.outcome_category == "AUTHORIZATION_DENIED"
    assert env.adapter.calls == env.provider.calls == []
    before = snapshot(env)
    env.types.register(definition(B, PUBLIC))
    assert snapshot(env) == before and not env.types.supports_type_version(KIND, A)
    forbid_effects(env, monkeypatch)
    surface(env.rt.execute(ReadObject("target"), actor=requester))
    assert snapshot(env) == before and env.adapter.calls == env.provider.calls == []


@pytest.mark.parametrize("requirements", [(SECRET, PUBLIC), (PUBLIC, SECRET)], ids=["weaker", "stronger"])
@pytest.mark.parametrize("grant", ["neither", "original", "replacement", "both"])
@pytest.mark.parametrize("advanced", [False, True], ids=["initial-history", "advanced-history"])
def test_replacement_counterexample_is_denied_without_effects(env, monkeypatch, requirements, grant, advanced):
    old, new = requirements
    env.types.register(definition(A, old))
    if advanced:
        assert env.rt.execute(TransitionState("target", "finish"), actor=env.owner).decision == Decision.ALLOW
    caps = {"neither": [], "original": [old], "replacement": [new], "both": [old, new]}[grant]
    before = snapshot(env)
    env.types.register(definition(B, new))
    assert snapshot(env) == before and env.store.get("target").type_version == A
    assert not env.types.supports_type_version(KIND, A)
    forbid_effects(env, monkeypatch)
    # Actual authority must not be evaluated using B, including by shared shaping.
    monkeypatch.setattr(env.rt._policy, "evaluate", lambda *a, **k: pytest.fail("replacement policy evaluated"))
    surface(env.rt.execute(ReadObject("target", fields=["secret"]), actor=actor(caps)))
    assert snapshot(env) == before and env.adapter.calls == env.provider.calls == []


def test_supported_unreadable_and_unsupported_surfaces_are_identical(env, monkeypatch):
    requester = actor([PUBLIC])
    context = CommandContext(command_id="same-command", correlation_id="same-correlation", application_id="full", expected_version=999)
    forbid_effects(env, monkeypatch)
    before = snapshot(env)
    supported = surface(env.rt.execute(ReadObject("target", fields=["secret"]), actor=requester, command_context=context))
    env.types.register(definition(B, PUBLIC))
    unsupported = surface(env.rt.execute(ReadObject("target", fields=["secret"]), actor=requester, command_context=context))
    assert supported == unsupported and snapshot(env) == before


def test_unsupported_surface_is_independent_of_protected_state_version_and_history(env, monkeypatch):
    requester = actor([PUBLIC])
    context = CommandContext(command_id="same-request", correlation_id="same-correlation", expected_version=999)
    def refused():
        with monkeypatch.context() as scoped:
            forbid_effects(env, scoped)
            before = snapshot(env)
            result = surface(env.rt.execute(ReadObject("target"), actor=requester, command_context=context))
            assert snapshot(env) == before and env.adapter.calls == env.provider.calls == []
            return result, before
    env.types.register(definition(B, PUBLIC))
    initial, before = refused()
    # Restore the matching definition to perform a real accepted transition;
    # do not rewrite the object's binding or manufacture its canonical history.
    env.adapter.block = env.provider.block = False
    env.types.register(definition(A, SECRET))
    assert env.rt.execute(TransitionState("target", "finish"), actor=env.owner).decision == Decision.ALLOW
    env.types.register(definition(B, PUBLIC))
    advanced, after = refused()
    assert before["object"]["state"] != after["object"]["state"]
    assert before["object"]["version"] != after["object"]["version"]
    assert len(before["history"]) == 1 and len(after["history"]) == 2
    assert before["object"]["type_version"] == after["object"]["type_version"] == A
    assert initial == advanced


def test_supported_positive_partial_field_missing_and_provider_failure_controls(env):
    before = snapshot(env)
    reader = actor([SECRET])
    partial = env.rt.execute(ReadObject("target", fields=["public", "secret"]), actor=reader)
    assert partial.decision == Decision.ALLOW and partial.data == {"public": "visible"}
    assert partial.reason == "partial read"
    denied_field = env.rt.execute(ReadObject("target", fields=["secret"]), actor=reader)
    assert denied_field.deny_reason is None and denied_field.reason == "access denied" and denied_field.outcome_category == "SEMANTICALLY_INVALID"
    missing_field = env.rt.execute(ReadObject("target", fields=["unknown"]), actor=reader)
    assert missing_field.outcome_category == "ACCEPTED" and missing_field.data == {}
    missing = env.rt.execute(ReadObject("absent"), actor=reader)
    assert missing.deny_reason == "NOT_FOUND" and missing.outcome_category == "SEMANTICALLY_INVALID"
    full = env.rt.execute(ReadObject("target"), actor=env.owner)
    assert full.decision == Decision.ALLOW and full.data == {"public": "visible", "secret": "protected"}
    env.provider.unavailable = True
    failed = env.rt.execute(ReadObject("target"), actor=reader)
    assert failed.outcome_category == "REFUSED" and failed.reason == "PROVIDER_UNAVAILABLE"
    assert snapshot(env) == before


@pytest.mark.parametrize("restriction", ["application", "mandate-capability", "mandate-revoked", "purpose", "scope"])
def test_supported_current_bounds_still_deny(env, monkeypatch, restriction):
    requester = actor([SECRET, PUBLIC])
    if restriction == "application":
        context = CommandContext(application_id="empty")
    else:
        env.store.save(GovernedObject(id="mandate", type="DelegatedMandate", state="revoked" if restriction == "mandate-revoked" else "active",
            data={"capabilities": [] if restriction == "mandate-capability" else [SECRET, PUBLIC],
                  "scope": {"object_ids": ["other"] if restriction == "scope" else ["target"]}, "purposes": ["approved"]}))
        env.store.add(Relationship(source_id="mandate", relation="delegated_by", target_id="principal", metadata={'source': 'canonical'}))
        env.store.add(Relationship(source_id="mandate", relation="delegated_to", target_id="requester", metadata={'source': 'canonical'}))
        context = CommandContext(mandate_id="mandate", purpose="wrong" if restriction == "purpose" else "approved")
    before = snapshot(env)
    forbid_effects(env, monkeypatch)
    surface(env.rt.execute(ReadObject("target"), actor=requester, command_context=context))
    assert snapshot(env) == before and env.adapter.calls == env.provider.calls == []


@pytest.mark.parametrize("bound", ["application", "mandate"])
def test_supported_current_bounds_positive_controls(env, bound):
    if bound == "application":
        context = CommandContext(application_id="full")
    else:
        env.store.save(GovernedObject(id="mandate", type="DelegatedMandate", state="active", data={
            "capabilities": [SECRET, PUBLIC], "scope": {"object_ids": ["target"]}, "purposes": ["approved"]}))
        env.store.add(Relationship(source_id="mandate", relation="delegated_by", target_id="principal", metadata={'source': 'canonical'}))
        env.store.add(Relationship(source_id="mandate", relation="delegated_to", target_id="requester", metadata={'source': 'canonical'}))
        context = CommandContext(application_id="full", mandate_id="mandate", purpose="approved")
    before = snapshot(env)
    result = env.rt.execute(ReadObject("target"), actor=env.owner, command_context=context)
    assert result.decision == Decision.ALLOW and result.data == {"public": "visible", "secret": "protected"}
    assert env.adapter.calls == ["hydrate", "filter"] and env.provider.calls == ["provider"]
    assert snapshot(env) == before


def test_unresolved_kind_denies_through_public_configuration(env, monkeypatch):
    build = GovernedObjectRuntime if env.entry == "direct" else GovernedObjectRuntime.configured
    rt = build(store=env.store, type_registry=TypeRegistry(), adapters=[env.adapter],
               extension_registry=TypeExtensionRegistry(), providers=ProviderRegistry(), application_registry=ApplicationRegistry())
    before = snapshot(env)
    forbid_effects(env, monkeypatch)
    surface(rt.execute(ReadObject("target"), actor=env.owner))
    assert not rt._requester_can_read(env.owner, env.store.get("target"), None)
    assert snapshot(env) == before and env.adapter.calls == env.provider.calls == []


@pytest.mark.parametrize("context", [CommandContext(application_id="missing"), CommandContext(mandate_id="missing"), CommandContext(mandate_id="missing", application_id="full")])
def test_independent_invalid_context_is_binding_independent(env, monkeypatch, context):
    forbid_effects(env, monkeypatch)
    requester = actor([SECRET, PUBLIC])
    before = snapshot(env)
    supported = env.rt.execute(ReadObject("target"), actor=requester, command_context=context)
    env.types.register(definition(B, PUBLIC))
    unsupported = env.rt.execute(ReadObject("target"), actor=requester, command_context=context)
    assert supported.model_dump() == unsupported.model_dump()
    assert supported.outcome_category == "AUTHORIZATION_DENIED" and supported.deny_reason in {"APPLICATION_UNKNOWN", "MANDATE_UNKNOWN"}
    assert supported.events == [] and supported.data == {} and supported.object_version is None
    assert snapshot(env) == before and env.adapter.calls == env.provider.calls == []


def test_shared_shaping_never_uses_replacement_permission(env, monkeypatch):
    env.types.register(definition(B, PUBLIC))
    forbid_effects(env, monkeypatch)
    requester = actor([PUBLIC])
    monkeypatch.setattr(env.rt._policy, "evaluate", lambda *a, **k: pytest.fail("replacement policy used for diagnostic permission"))
    before = snapshot(env)
    result = env.rt.execute(AddRelationship("target", "link", "other"), actor=requester)
    assert result.decision == Decision.DENY and result.outcome_category == "SEMANTICALLY_INVALID"
    assert result.deny_reason == "TYPE_VERSION_UNSUPPORTED"  # unrelated admission/classification preserved
    assert result.object_version is None and result.actual_version is None and result.previous_state is None and result.new_state is None
    assert result.data == {} and result.events == [] and snapshot(env) == before
    raw = CommandResult(decision=Decision.DENY, action="update", deny_reason="STATE_INVALID", data={"secret": "protected"},
                        object_version=99, actual_version=99, previous_state="private", new_state="private",
                        validation_checks=[ValidationCheck(name="private-policy-field", passed=False, message="secret")])
    shaped = env.rt._shape_for_requester(raw, TransitionState("target", "finish"), requester, None)
    assert shaped.outcome_category == "SEMANTICALLY_INVALID" and shaped.deny_reason == "STATE_INVALID"
    assert shaped.data == {} and shaped.events == [] and shaped.validation_checks == [] and shaped.object_version is None


def test_supported_authorized_shaping_keeps_existing_diagnostics(env):
    raw = CommandResult(decision=Decision.DENY, action="update", deny_reason="STATE_INVALID", object_version=1,
                        previous_state="draft", reason="specific permitted diagnostic")
    shaped = env.rt._shape_for_requester(raw, TransitionState("target", "finish"), env.owner, None)
    assert shaped.object_version == 1 and shaped.previous_state == "draft" and shaped.reason == "specific permitted diagnostic"
