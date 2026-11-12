# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Brafford Labs, LLC
"""Generic admission, exact external configuration and required-input refusal."""
from dataclasses import replace

import pytest

from governed_objects.adapters.mandate import MandateAdapter
from governed_objects.delegation.configuration import DelegationConfiguration, DelegationConfigurationError
from governed_objects.delegation.scope import MandateScope
from governed_objects.delegation.service import MandateValidationError
from governed_objects.domain.command import AddRelationship, CreateObject, RemoveRelationship, TransitionState
from governed_objects.domain.command_context import CommandContext
from governed_objects.events.reducers.mandate import MandateEventReducer
from governed_objects.applications.registry import ApplicationDefinition
from governed_objects.policy.evaluator import CapabilityPolicyEvaluator
from governed_objects.agent.session import GovernedAgentSession
from governed_objects.policy.context import AuthorizationContextError
from tests.fixtures.delegation import DELEGATE, OWNER, build, create, session, snapshot


@pytest.fixture
def rt(tmp_path):
    runtime, adapter = build(tmp_path/'admission.db')
    yield runtime, adapter
    runtime._store.close()


def refused(runtime, adapter, operation):
    before = snapshot(runtime)
    adapter.effects.clear()
    result = operation()
    assert result.decision == 'DENY'
    assert snapshot(runtime) == before
    assert adapter.effects == []
    return result


@pytest.mark.parametrize('data', [
    {'scope': []}, {'scope': ['bad']}, {'scope': {'future_required_zone': 'never'}},
    {'scope': {'object_ids': None}}, {'scope': {'object_ids': 'a'}},
    {'scope': {'object_types': [False]}}, {'scope': {'relationship_constraints': {}}},
    {'scope': {'relationship_constraints': [{'relation': 'zone'}]}},
    {'scope': {'relationship_constraints': [{'relation': '', 'target_id': 'z'}]}},
    {'valid_from': ''}, {'valid_until': False}, {'valid_until': 'not-a-date'},
    {'valid_from': '2026-10-28T12:00:00'},
    {'valid_from': '2026-10-29T12:00:00Z', 'valid_until': '2026-10-28T12:00:00Z'},
    {'capabilities': []}, {'capabilities': 'signal.read'}, {'purposes': False},
    {'parent_mandate_id': ''}, {'parent_mandate_id': 'absent'}, {'parent_mandate_id': 'p'},
    {'future_required_bound': 'never'},
])
def test_generic_create_refuses_malformed_required_inputs_before_acceptance(rt, data):
    runtime, adapter = rt
    refused(runtime, adapter, lambda: create(runtime, **data))
    assert runtime._store.get('p') is None
    assert runtime._store.version_high_water('p') is None
    assert runtime.accepted_history('p') == []


@pytest.mark.parametrize('encoding', ['status', 'no-principal', 'no-delegate', 'multiple-principals',
                                      'multiple-delegates', 'conflicting-parent', 'multiple-parents',
                                      'wrong-source', 'missing-target'])
def test_generic_create_refuses_malformed_relationships_and_status(rt, encoding):
    runtime, adapter = rt
    rels = [{'source_id': 'p', 'relation': 'delegated_by', 'target_id': OWNER.id},
            {'source_id': 'p', 'relation': 'delegated_to', 'target_id': DELEGATE.id}]
    data = {'capabilities': ['signal.read'], 'scope': {}}
    if encoding == 'no-principal':
        rels.pop(0)
    elif encoding == 'no-delegate':
        rels.pop()
    elif encoding.startswith('multiple-'):
        relation = {'multiple-principals': 'delegated_by', 'multiple-delegates': 'delegated_to',
                    'multiple-parents': 'derived_from'}[encoding]
        if relation == 'derived_from':
            rels.append({'source_id': 'p', 'relation': relation, 'target_id': 'one'})
        rels.append({'source_id': 'p', 'relation': relation, 'target_id': 'two'})
    elif encoding == 'conflicting-parent':
        data['parent_mandate_id'] = 'one'
        rels.append({'source_id': 'p', 'relation': 'derived_from', 'target_id': 'two'})
    elif encoding == 'wrong-source':
        rels[0]['source_id'] = 'other'
    elif encoding == 'missing-target':
        del rels[0]['target_id']
    refused(runtime, adapter, lambda: runtime.execute(CreateObject(
        'p', 'DelegatedMandate', data, state='suspended' if encoding == 'status' else 'active',
        relationships=rels), actor=OWNER))


@pytest.mark.parametrize('change', ['remove-delegate', 'second-delegate', 'second-principal',
                                   'cycle', 'missing-parent', 'legacy-without-decoder'])
def test_generic_relationship_changes_validate_before_acceptance(rt, change):
    runtime, adapter = rt
    assert create(runtime).decision == 'ALLOW'
    command = {
        'remove-delegate': RemoveRelationship('p', 'delegated_to', DELEGATE.id),
        'second-delegate': AddRelationship('p', 'delegated_to', 'worker'),
        'second-principal': AddRelationship('p', 'delegated_by', 'worker'),
        'cycle': AddRelationship('p', 'derived_from', 'p'),
        'missing-parent': AddRelationship('p', 'derived_from', 'absent'),
        'legacy-without-decoder': AddRelationship('p', 'scoped_to', 'a'),
    }[change]
    refused(runtime, adapter, lambda: runtime.execute(command, actor=OWNER,
                                                     command_context=CommandContext(expected_version=1)))


@pytest.mark.parametrize('dimension', ['disjoint-scope', 'cross-types', 'cross-relationship', 'purpose', 'caps', 'time'])
def test_generic_administration_cannot_bypass_service_narrowing(rt, dimension):
    runtime, adapter = rt
    assert create(runtime, scope={'object_ids': ['a']}, purposes=['work'],
                  valid_until='2026-10-28T13:00:00Z').decision == 'ALLOW'
    child = dict(scope={}, purposes=[], valid_until=None)
    if dimension == 'disjoint-scope':
        child['scope'] = {'object_ids': ['b']}
    elif dimension == 'cross-types':
        child['scope'] = {'object_types': ['Signal']}
    elif dimension == 'cross-relationship':
        for oid in ('a', 'b'):
            assert runtime.execute(AddRelationship(oid, 'zone', 'z'), actor=OWNER,
                                   command_context=CommandContext(expected_version=1)).decision == 'ALLOW'
        child['scope'] = {'relationship_constraints': [{'relation': 'zone', 'target_id': 'z'}]}
    elif dimension == 'purpose':
        child['purposes'] = ['different']
    elif dimension == 'caps':
        child['capabilities'] = ['signal.read', 'signal.finish', 'mandate.revoke']
    else:
        child['valid_until'] = '2026-10-29T12:00:00Z'
    refused(runtime, adapter, lambda: session(runtime).execute(TransitionState('b', 'finish'), expected_version=runtime._store.get('b').version))
    refused(runtime, adapter, lambda: create(runtime, 'child', 'p', **child))


@pytest.mark.parametrize('broken', ['missing-profile', 'replacement-definition', 'version', 'adapter', 'reducer', 'trust', 'decoder'])
@pytest.mark.parametrize('entry', ['command', 'query', 'traverse'])
def test_current_profile_mismatch_and_unready_context_refuse_before_effects(rt, broken, entry):
    runtime, adapter = rt
    assert create(runtime).decision == 'ALLOW'
    captured = session(runtime).auth
    config = runtime._delegation
    if broken == 'missing-profile':
        runtime._delegation = None
    elif broken == 'replacement-definition':
        runtime._types.register(config.definition.model_copy(deep=True))
    elif broken == 'version':
        runtime._types.register(config.definition.model_copy(update={'type_version': 'unsupported:v2'}))
    elif broken == 'adapter':
        runtime._adapters_list.remove(config.adapter)
        runtime._adapters_list.append(MandateAdapter())
    elif broken == 'reducer':
        runtime._extensions.register_reducer('DelegatedMandate', MandateEventReducer())
    elif broken == 'trust':
        config.actor_source.__self__.pop(OWNER.id)
    else:
        runtime._delegation = replace(config, scope_decoder=lambda ids, store: [])
    before = snapshot(runtime)
    adapter.effects.clear()
    if entry == 'command':
        assert session(runtime).execute(TransitionState('a', 'finish'), expected_version=1).decision == 'DENY'
    else:
        with pytest.raises(AuthorizationContextError, match='^authorization context unavailable$'):
            if entry == 'query':
                runtime.query(DELEGATE, object_type='Signal', auth=captured)
            else:
                runtime.traverse(DELEGATE, 'a', max_depth=0, auth=captured)
    assert snapshot(runtime) == before
    assert adapter.effects == []


def test_absent_optional_profile_preserves_ordinary_core_and_refuses_administration(tmp_path):
    runtime, adapter = build(tmp_path/'core.db', configured=False)
    assert runtime.query(DELEGATE, object_type='Signal')
    refused(runtime, adapter, lambda: create(runtime))
    before = snapshot(runtime)
    with pytest.raises(MandateValidationError, match='delegation configuration unavailable'):
        runtime.mandates.create_mandate(mandate_id='p', principal=OWNER, delegate_actor_id=DELEGATE.id,
                                        capabilities=frozenset({'signal.read'}))
    assert snapshot(runtime) == before
    assert runtime.execute(TransitionState('a', 'finish'), actor=OWNER,
                           command_context=CommandContext(expected_version=1)).decision == 'ALLOW'
    runtime._store.close()


@pytest.mark.parametrize('part', ['definition', 'version', 'adapter', 'reducer', 'trust', 'decoder'])
def test_conflicting_or_partial_descriptor_is_rejected_immediately(rt, part):
    runtime, adapter = rt
    config = runtime._delegation
    value = {
        'definition': config.definition.model_copy(update={'read_requires': []}),
        'version': config.definition.model_copy(update={'type_version': 'v-next'}),
        'adapter': object(), 'reducer': object(), 'trust': None, 'decoder': None,
    }[part]
    field = {'version': 'definition', 'trust': 'actor_source', 'decoder': 'scope_decoder'}.get(part, part)
    before = snapshot(runtime)
    with pytest.raises(DelegationConfigurationError):
        replace(config, **{field: value})
    assert snapshot(runtime) == before


def test_unknown_required_actor_constraint_is_not_a_noop(rt):
    runtime, adapter = rt
    actor = OWNER.model_copy(update={'constraints': ['future_deny_all']})
    refused(runtime, adapter, lambda: runtime.execute(TransitionState('a', 'finish'), actor=actor,
                                                     command_context=CommandContext(expected_version=1)))


@pytest.mark.parametrize('restriction', ['revoked', 'application'])
def test_direct_default_evaluator_cannot_preserve_a_stale_authority_snapshot(rt, restriction):
    runtime, adapter = rt
    assert create(runtime).decision == 'ALLOW'
    captured = session(runtime).auth
    obj = runtime._store.get('a')
    definition = runtime._types.get('Signal')
    kwargs = dict(application=captured.application, mandate=captured.mandate,
                  resolved_mandate=captured.resolved_mandate, purpose='work')
    assert runtime._policy.evaluate(DELEGATE, 'finish', obj, definition, **kwargs).decision == 'ALLOW'
    if restriction == 'revoked':
        runtime.mandates.revoke('p', actor=OWNER)
    else:
        runtime._applications.register(ApplicationDefinition('full', 'full', capabilities=frozenset()))
    before = snapshot(runtime)
    adapter.effects.clear()
    assert runtime._policy.evaluate(DELEGATE, 'finish', obj, definition, **kwargs).decision == 'DENY'
    assert snapshot(runtime) == before
    assert adapter.effects == []


def test_standalone_evaluator_requires_explicit_authority_sources_not_snapshots(rt):
    runtime, adapter = rt
    assert create(runtime).decision == 'ALLOW'
    evaluator = CapabilityPolicyEvaluator(store=runtime._store)
    captured = session(runtime).auth
    obj = runtime._store.get('a')
    definition = runtime._types.get('Signal')
    assert evaluator.evaluate(DELEGATE, 'finish', obj, definition).decision == 'ALLOW'
    assert evaluator.evaluate(DELEGATE, 'finish', obj, definition,
                              resolved_mandate=captured.resolved_mandate, purpose='work').decision == 'DENY'
    assert evaluator.evaluate(DELEGATE, 'finish', obj, definition, application=captured.application).decision == 'DENY'


@pytest.mark.parametrize('field,value', [('application_id', None), ('mandate_id', None),
    ('application_id', False), ('mandate_id', []), ('application_id', ''), ('actor', None), ('purpose', False)])
def test_required_session_inputs_cannot_become_optional_base_authority(rt, field, value):
    runtime, adapter = rt
    before = snapshot(runtime)
    adapter.effects.clear()
    kwargs = dict(actor=DELEGATE, application_id='full', mandate_id='p', purpose='work')
    kwargs[field] = value
    with pytest.raises(AuthorizationContextError, match='^authorization context unavailable$'):
        GovernedAgentSession(runtime, **kwargs)
    assert snapshot(runtime) == before
    assert adapter.effects == []
