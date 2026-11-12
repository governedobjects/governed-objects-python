# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Brafford Labs, LLC
"""Test-owned local authority inputs, with no business/reference dependencies."""
from datetime import UTC, datetime

from governed_objects.adapters.base import TypeAdapter
from governed_objects.adapters.mandate import MandateAdapter
from governed_objects.agent.session import GovernedAgentSession
from governed_objects.applications.registry import ApplicationDefinition, ApplicationRegistry
from governed_objects.domain.clock import FixedClock
from governed_objects.domain.command import CreateObject
from governed_objects.domain.models import Actor, ActorType
from governed_objects.events.reducers.mandate import MandateEventReducer
from governed_objects.delegation.configuration import DelegationConfiguration
from governed_objects.delegation.materializer import reject_legacy_scope
from governed_objects.persistence.sqlite import SQLiteStore
from governed_objects.providers.registry import ProviderRegistry
from governed_objects.runtime.runtime import GovernedObjectRuntime
from governed_objects.types.extensions import TypeExtensionRegistry
from governed_objects.types.lifecycle import LifecycleValidator
from governed_objects.types.registry import TypeRegistry
from governed_objects.types.schema import ObjectTypeDefinition
from governed_objects.types.validation import SchemaValidator

NOW = datetime(2026, 10, 28, 12, tzinfo=UTC)
CAPS = frozenset({'signal.read', 'signal.finish', 'mandate.create', 'mandate.activate',
                  'mandate.revoke', 'mandate.read', 'relationship.add', 'relationship.remove'})
OWNER = Actor(id='owner', type=ActorType.HUMAN, capabilities=sorted(CAPS))
DELEGATE = Actor(id='delegate', type=ActorType.AGENT, capabilities=sorted(CAPS))
WORKER = Actor(id='worker', type=ActorType.AGENT, capabilities=sorted(CAPS))


def mandate_definition():
    return ObjectTypeDefinition(
        kind='DelegatedMandate', type_version='1', initial_state='draft',
        fields={name: {'type': kind, 'required': name == 'capabilities'} for name, kind in (
            ('capabilities', 'array'), ('purposes', 'array'), ('valid_from', 'string'),
            ('valid_until', 'string'), ('parent_mandate_id', 'string'))},
        read_requires=['mandate.read'],
        actions={
            **{action: {'requires': ['mandate.' + action]} for action in ('create', 'activate', 'revoke')},
            'add_relationship': {'requires': ['relationship.add']},
            'remove_relationship': {'requires': ['relationship.remove']},
        },
        states={'draft': {'transitions': {'activate': 'active'}},
                'active': {'transitions': {'revoke': 'revoked'}}, 'revoked': {'terminal': True}},
    )


class SignalAdapter(TypeAdapter):
    def __init__(self):
        self.effects = []

    def supports(self, kind):
        return kind in ('Signal', 'Other')

    def validate_create(self, definition, data):
        valid, checks, errors = SchemaValidator().validate(definition, data)
        return valid, checks, '; '.join(errors)

    def validate_transition(self, obj, definition, action, actor):
        valid, reason, check = LifecycleValidator().validate_transition(definition, obj.state, action)
        return valid, [check], reason

    def load(self, obj, context):
        self.effects.append(('load', obj.id))
        return None

    def filter_read_data(self, obj, definition, actor, fields):
        self.effects.append(('filter', obj.id))
        return obj.data, []

    def adapter_execute(self, obj, command, context):
        self.effects.append(('execute', obj.id))
        return None


def build(path, *, configured=True, scope_decoder=reject_legacy_scope):
    store = SQLiteStore(path)
    types = TypeRegistry()
    for kind in ('Signal', 'Other'):
        types.register(ObjectTypeDefinition(
            kind=kind, type_version='signal:v1', initial_state='open',
            states={'open': {'transitions': {'finish': 'done'}}, 'done': {'terminal': True}},
            actions={'create': {'requires': [], 'open': True}, 'finish': {'requires': ['signal.finish']},
                     'add_relationship': {'requires': ['relationship.add']},
                     'remove_relationship': {'requires': ['relationship.remove']}},
            read_requires=['signal.read'],
        ))
    types.register(mandate_definition())
    extensions = TypeExtensionRegistry()
    extensions.register_reducer('DelegatedMandate', MandateEventReducer())
    apps = ApplicationRegistry()
    apps.register(ApplicationDefinition('full', 'full', capabilities=CAPS))
    apps.register(ApplicationDefinition('empty', 'empty', capabilities=frozenset()))
    adapter = SignalAdapter()
    mandate_adapter = MandateAdapter()
    actors = {actor.id: actor for actor in (OWNER, DELEGATE, WORKER)}
    delegation = DelegationConfiguration(
        types.get('DelegatedMandate'), mandate_adapter, extensions.reducer_for('DelegatedMandate'),
        scope_decoder, actors.__getitem__,
    ) if configured else None
    runtime = GovernedObjectRuntime.configured(
        store=store, type_registry=types, adapters=[adapter, mandate_adapter],
        extension_registry=extensions, providers=ProviderRegistry(), application_registry=apps,
        clock=FixedClock(NOW),
        delegation=delegation,
    )
    for oid, kind in (('a', 'Signal'), ('b', 'Signal'), ('c', 'Other')):
        assert runtime.execute(CreateObject(oid, kind, {'payload': oid}), actor=OWNER).decision == 'ALLOW'
    return runtime, adapter


def session(runtime, mandate='p', actor=DELEGATE, app='full', purpose='work'):
    return GovernedAgentSession(runtime, actor=actor, application_id=app, mandate_id=mandate, purpose=purpose)


def create(runtime, mid='p', parent=None, **data):
    """Generic administration of well-formed declarations, not service issuance."""
    values = dict(capabilities=['signal.read', 'signal.finish'], purposes=['work'],
                  scope={}, parent_mandate_id=parent)
    values.update(data)
    return runtime.execute(CreateObject(mid, 'DelegatedMandate', values, state='active', relationships=[
        {'source_id': mid, 'relation': 'delegated_by', 'target_id': OWNER.id},
        {'source_id': mid, 'relation': 'delegated_to', 'target_id': DELEGATE.id},
    ]), actor=OWNER)


def snapshot(runtime):
    return {obj.id: dict(
        object=obj.model_dump(mode='json'), high_water=runtime._store.version_high_water(obj.id),
        accepted=[event.model_dump(mode='json') for event in runtime.accepted_history(obj.id)],
        relationships=[rel.model_dump(mode='json') for rel in runtime.canonical_relationships(obj.id)],
    ) for obj in runtime._store.list_all()}
