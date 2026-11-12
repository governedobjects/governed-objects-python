# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Brafford Labs, LLC
"""Owned M3 regressions for declaration/effective-bound and context separation."""
from datetime import timedelta

import pytest

from governed_objects.applications.registry import ApplicationDefinition
from governed_objects.delegation.scope import MandateScope, RelationshipConstraint, scope_allows
from governed_objects.delegation.service import MandateValidationError
from governed_objects.domain.command import AddRelationship, CreateObject, TransitionState
from governed_objects.domain.command_context import CommandContext
from governed_objects.policy.context import AuthorizationContext, AuthorizationContextError
from governed_objects.relationships.canonical import canonical_relationship
from tests.fixtures.delegation import (
    CAPS, DELEGATE, NOW, OWNER, WORKER, build, create, session, snapshot,
)


@pytest.fixture
def rt(tmp_path):
    def scoped_ids(targets, store):
        return MandateScope(object_ids=frozenset(targets))

    runtime, adapter = build(tmp_path / 'case.db', scope_decoder=scoped_ids)
    yield runtime, adapter
    runtime._store.close()


def issue(runtime, mid='p', principal=OWNER, delegate=DELEGATE, **kwargs):
    return runtime.mandates.create_mandate(
        mandate_id=mid, principal=principal, delegate_actor_id=delegate.id,
        capabilities=frozenset({'signal.read', 'signal.finish'}), **kwargs,
    )


@pytest.mark.parametrize('mode', ['cross-types', 'cross-relationship'])
def test_service_cannot_expand_from_one_matching_witness(rt, mode):
    runtime, adapter = rt
    issue(runtime, scope=MandateScope(object_ids=frozenset({'a'})), purposes=frozenset({'work'}))
    denied = session(runtime).execute(TransitionState('b', 'finish'), expected_version=1)
    assert denied.decision == 'DENY'
    if mode == 'cross-relationship':
        for oid in ('a', 'b'):
            assert runtime.execute(AddRelationship(oid, 'zone', 'z'), actor=OWNER,
                                   command_context=CommandContext(expected_version=1)).decision == 'ALLOW'
        scope = MandateScope(relationship_constraints=(RelationshipConstraint('zone', 'z'),))
    else:
        scope = MandateScope(object_types=frozenset({'Signal'}))
    before = snapshot(runtime)
    adapter.effects.clear()
    with pytest.raises(MandateValidationError) as error:
        issue(runtime, 'child', principal=DELEGATE, delegate=WORKER,
              parent_mandate_id='p', scope=scope, purposes=frozenset({'work'}))
    assert [v.value for v in error.value.violations] == ['SCOPE_EXPANSION']
    assert snapshot(runtime) == before
    assert adapter.effects == []


def test_service_inherited_omissions_preserve_declarations_and_history(rt):
    runtime, adapter = rt
    issue(runtime, scope=MandateScope(object_ids=frozenset({'a'})),
          purposes=frozenset({'work'}), valid_from=NOW-timedelta(hours=1), valid_until=NOW+timedelta(hours=1))
    issue(runtime, 'child', principal=DELEGATE, delegate=WORKER, parent_mandate_id='p')
    child = runtime._store.get('child')
    assert child.data['scope'] == {'object_ids': [], 'object_types': [], 'relationship_constraints': []}
    assert child.data['purposes'] == []
    assert child.data['valid_from'] is child.data['valid_until'] is None
    authority = runtime.mandate_resolver.resolve_authority('child')
    assert authority.purposes == frozenset({'work'})
    assert authority.valid_from == NOW-timedelta(hours=1)
    assert authority.valid_until == NOW+timedelta(hours=1)
    before = snapshot(runtime)
    adapter.effects.clear()
    assert session(runtime, 'child', WORKER).execute(TransitionState('b', 'finish'), expected_version=1).decision == 'DENY'
    assert snapshot(runtime) == before
    assert adapter.effects == []
    assert session(runtime, 'child', WORKER).execute(TransitionState('a', 'finish'), expected_version=1).decision == 'ALLOW'
    assert runtime._store.get('a').state == 'done'
    assert runtime._store.get('a').version == 2
    assert len(runtime.accepted_history('a')) == 2
    runtime._clock.advance_to(NOW+timedelta(hours=2))
    before = snapshot(runtime)
    adapter.effects.clear()
    assert session(runtime, 'child', WORKER).read('a').decision == 'DENY'
    assert snapshot(runtime) == before
    assert adapter.effects == []


@pytest.mark.parametrize('ancestor', ['p', 'g'])
@pytest.mark.parametrize('entry', ['command', 'query', 'traverse'])
def test_current_scope_of_each_ancestor_is_conjunctive_even_for_captured_auth(rt, ancestor, entry):
    runtime, adapter = rt
    for mid, parent in (('g', None), ('p', 'g'), ('child', 'p')):
        assert create(runtime, mid, parent).decision == 'ALLOW'
    caller = session(runtime, 'child')
    captured = caller.auth
    assert {obj.id for obj in runtime.query(DELEGATE, object_type='Signal', auth=captured)} == {'a', 'b'}
    assert runtime.execute(AddRelationship(ancestor, 'scoped_to', 'a'), actor=OWNER,
                           command_context=CommandContext(expected_version=1)).decision == 'ALLOW'
    before = snapshot(runtime)
    adapter.effects.clear()
    assert session(runtime, ancestor).read('b').decision == 'DENY'
    if entry == 'command':
        assert caller.execute(TransitionState('b', 'finish'), expected_version=1).decision == 'DENY'
    elif entry == 'query':
        assert runtime.query(DELEGATE, object_type='Signal', auth=captured)[0].id == 'a'
        assert all(oid != 'b' for _, oid in adapter.effects)
    else:
        assert runtime.traverse(DELEGATE, 'b', max_depth=0, auth=captured) == []
    assert snapshot(runtime) == before
    assert all(oid != 'b' for _, oid in adapter.effects)
    assert scope_allows(runtime.mandate_resolver.resolve_authority('child').scope, runtime._store.get('a'), runtime._store)


@pytest.mark.parametrize('entry', ['command', 'query', 'traverse'])
@pytest.mark.parametrize('ancestor', ['p', 'g'])
def test_captured_authority_does_not_survive_current_ancestor_revocation(rt, entry, ancestor):
    runtime, adapter = rt
    for mid, parent in (('g', None), ('p', 'g'), ('child', 'p')):
        assert create(runtime, mid, parent).decision == 'ALLOW'
    caller = session(runtime, 'child')
    captured = caller.auth
    assert caller.read('b').decision == 'ALLOW'
    assert runtime.mandates.revoke(ancestor, actor=OWNER) is not None
    before = snapshot(runtime)
    adapter.effects.clear()
    if entry == 'command':
        assert caller.execute(TransitionState('b', 'finish'), expected_version=1).decision == 'DENY'
    elif entry == 'query':
        assert runtime.query(DELEGATE, object_type='Signal', auth=captured) == []
    else:
        assert runtime.traverse(DELEGATE, 'b', max_depth=0, auth=captured) == []
    assert snapshot(runtime) == before
    assert adapter.effects == []


@pytest.mark.parametrize('missing', ['mandate', 'application'])
@pytest.mark.parametrize('entry', ['auth', 'query', 'traverse'])
def test_missing_session_context_is_not_dropped(rt, missing, entry):
    runtime, adapter = rt
    issue(runtime)
    caller = session(runtime, mandate='absent' if missing == 'mandate' else 'p',
                     app='absent' if missing == 'application' else 'full')
    before = snapshot(runtime)
    adapter.effects.clear()
    with pytest.raises(AuthorizationContextError, match='^authorization context unavailable$'):
        if entry == 'auth':
            caller.auth
        elif entry == 'query':
            caller.query(object_type='Signal')
        else:
            caller.traverse('a', max_depth=0)
    assert snapshot(runtime) == before
    assert adapter.effects == []
    assert caller.read('a').decision == 'DENY'


@pytest.mark.parametrize('entry', ['query', 'traverse'])
def test_captured_application_is_reresolved_at_use(rt, entry):
    runtime, adapter = rt
    issue(runtime)
    captured = session(runtime).auth
    runtime._applications.register(ApplicationDefinition('full', 'full', capabilities=frozenset()))
    before = snapshot(runtime)
    adapter.effects.clear()
    result = (runtime.query(DELEGATE, object_type='Signal', auth=captured) if entry == 'query'
              else runtime.traverse(DELEGATE, 'a', max_depth=0, auth=captured))
    assert result == []
    assert snapshot(runtime) == before
    assert adapter.effects == []


@pytest.mark.parametrize('levels,allowed', [(4, True), (5, False), (6, False)])
def test_finite_depth_refuses_unresolved_tail(rt, levels, allowed):
    runtime, adapter = rt
    for level in range(levels+1):
        before = snapshot(runtime)
        result = create(runtime, f'm{level}', f'm{level-1}' if level else None)
        if level <= 4:
            assert result.decision == 'ALLOW'
        else:
            assert result.decision == 'DENY'
            assert snapshot(runtime) == before
            # Separate stored-corruption backstop, not accepted generic issuance.
            template = runtime._store.get('m4')
            runtime._store.save(template.model_copy(update={
                'id': f'm{level}', 'data': dict(template.data, parent_mandate_id=f'm{level-1}'),
            }))
            for relation, identity in (('delegated_by', OWNER.id), ('delegated_to', DELEGATE.id)):
                runtime._store.add(canonical_relationship(f'm{level}', relation, identity))
    before = snapshot(runtime)
    adapter.effects.clear()
    authority = runtime.mandate_resolver.resolve_authority(f'm{levels}')
    assert (authority.ancestor_ineffective_reason is None) == allowed
    result = session(runtime, f'm{levels}').read('a')
    assert (result.decision == 'ALLOW') == allowed
    assert snapshot(runtime) == before
    if not allowed:
        assert authority.ancestor_ineffective_reason == 'DELEGATION_DEPTH_EXCEEDED'
        assert adapter.effects == []


@pytest.mark.parametrize('entry', ['query', 'traverse'])
def test_unsupported_target_version_has_no_hydration_or_filter_effect(rt, entry):
    runtime, adapter = rt
    definition = runtime._types.get('Signal')
    runtime._types.register(definition.model_copy(update={'type_version': 'incompatible'}))
    before = snapshot(runtime)
    adapter.effects.clear()
    result = (runtime.query(DELEGATE, object_type='Signal') if entry == 'query'
              else runtime.traverse(DELEGATE, 'a', max_depth=0))
    assert result == []
    assert snapshot(runtime) == before
    assert adapter.effects == []


def test_neutral_relationship_scope_matches_all_current_targets(rt):
    runtime, adapter = rt
    for index, target in enumerate(('z1', 'z2')):
        assert runtime.execute(AddRelationship('a', 'zone', target), actor=OWNER,
                               command_context=CommandContext(expected_version=1+index)).decision == 'ALLOW'
    issue(runtime, scope=MandateScope(relationship_constraints=(RelationshipConstraint('zone', 'z1'),)))
    assert session(runtime).read('a').decision == 'ALLOW'
    assert session(runtime).read('b').decision == 'DENY'


@pytest.mark.parametrize('restriction', ['actor', 'application', 'delegate'])
def test_independent_authority_intersections_are_preserved(rt, restriction):
    runtime, adapter = rt
    issue(runtime)
    actor = DELEGATE.model_copy(update={'capabilities': ['signal.read']}) if restriction == 'actor' else DELEGATE
    if restriction == 'delegate':
        actor = OWNER
    caller = session(runtime, actor=actor, app='empty' if restriction == 'application' else 'full')
    before = snapshot(runtime)
    adapter.effects.clear()
    assert caller.execute(TransitionState('a', 'finish'), expected_version=1).decision == 'DENY'
    assert snapshot(runtime) == before
    assert adapter.effects == []


def test_restricted_scope_cannot_authorize_unknown_prospective_target(rt):
    runtime, adapter = rt
    issue(runtime, scope=MandateScope(object_ids=frozenset({'a'})))
    before = snapshot(runtime)
    adapter.effects.clear()
    assert session(runtime).execute(CreateObject('new', 'Signal', {})).decision == 'DENY'
    assert snapshot(runtime) == before
    assert adapter.effects == []


@pytest.mark.parametrize('bound', ['caps', 'scope', 'purpose', 'future', 'expired', 'revoked', 'draft'])
def test_every_ancestor_bound_protects_use_of_preexisting_declarations(rt, bound):
    """Defensive use-time control. Direct saved-data fixture, NOT admission proof."""
    runtime, adapter = rt
    for mid, parent in (('g', None), ('p', 'g'), ('child', 'p')):
        assert create(runtime, mid, parent).decision == 'ALLOW'
    ancestor = runtime._store.get('g')
    values = dict(ancestor.data)
    state = ancestor.state
    if bound == 'caps':
        values['capabilities'] = ['signal.read']
    elif bound == 'scope':
        values['scope'] = {'object_ids': ['a']}
    elif bound == 'purpose':
        values['purposes'] = ['root-only']
    elif bound == 'future':
        values['valid_from'] = (NOW+timedelta(days=1)).isoformat()
    elif bound == 'expired':
        values['valid_until'] = (NOW-timedelta(days=1)).isoformat()
    else:
        state = bound
    runtime._store.save(ancestor.model_copy(update={'data': values, 'state': state}))
    before = snapshot(runtime)
    adapter.effects.clear()
    assert session(runtime, 'g').execute(TransitionState('b', 'finish'), expected_version=1).decision == 'DENY'
    assert session(runtime, 'child').execute(TransitionState('b', 'finish'), expected_version=1).decision == 'DENY'
    assert snapshot(runtime) == before
    assert adapter.effects == []
    if bound == 'caps':
        assert session(runtime, 'child').read('a').decision == 'ALLOW'
    elif bound == 'scope':
        assert session(runtime, 'child').read('a').decision == 'ALLOW'


@pytest.mark.parametrize('dimension', ['scope', 'purposes'])
def test_disjoint_preexisting_bounds_never_become_unrestricted(rt, dimension):
    """Direct saved-data backstop; generic admission closure is a separate gate."""
    runtime, adapter = rt
    for mid, parent in (('p', None), ('child', 'p')):
        assert create(runtime, mid, parent).decision == 'ALLOW'
    for mid, value in (('p', 'a'), ('child', 'b')):
        obj = runtime._store.get(mid)
        data = dict(obj.data)
        data[dimension] = {'object_ids': [value]} if dimension == 'scope' else [value]
        runtime._store.save(obj.model_copy(update={'data': data}))
    before = snapshot(runtime)
    adapter.effects.clear()
    for target in ('a', 'b', 'c'):
        caller = session(runtime, 'child', purpose='b')
        assert caller.read(target).decision == 'DENY'
        assert caller.execute(TransitionState(target, 'finish'), expected_version=1).decision == 'DENY'
    assert snapshot(runtime) == before
    assert adapter.effects == []


@pytest.mark.parametrize('broken', ['cycle', 'missing'])
def test_unresolved_preexisting_chain_is_ineffective(rt, broken):
    runtime, adapter = rt
    for mid, parent in (('p', None), ('child', 'p')):
        assert create(runtime, mid, parent).decision == 'ALLOW'
    obj = runtime._store.get('p')
    data = dict(obj.data, parent_mandate_id='child' if broken == 'cycle' else 'absent')
    runtime._store.save(obj.model_copy(update={'data': data}))
    before = snapshot(runtime)
    adapter.effects.clear()
    assert session(runtime, 'child').execute(TransitionState('b', 'finish'), expected_version=1).decision == 'DENY'
    assert snapshot(runtime) == before
    assert adapter.effects == []


@pytest.mark.parametrize('field,value', [
    ('scope', []), ('scope', ['bad']), ('scope', {'future_required_zone': 'never'}),
    ('scope', {'object_ids': None}), ('scope', {'object_ids': 'a'}),
    ('scope', {'object_types': [False]}),
    ('scope', {'relationship_constraints': [{'relation': 'zone'}]}),
    ('scope', {'relationship_constraints': [{'relation': '', 'target_id': 'z'}]}),
    ('scope', {'relationship_constraints': {}}),
    ('valid_from', ''), ('valid_until', 'not-a-date'), ('valid_until', False),
    ('valid_from', '2026-10-28T13:00:00'), ('capabilities', []),
    ('capabilities', 'signal.read'), ('purposes', False), ('parent_mandate_id', ''),
    ('future_required_bound', 'never'),
])
def test_malformed_preexisting_data_refuses_without_incidental_exceptions(rt, field, value):
    """Stored corruption backstop, not a substitute for pre-persistence validation."""
    runtime, adapter = rt
    assert create(runtime).decision == 'ALLOW'
    obj = runtime._store.get('p')
    runtime._store.save(obj.model_copy(update={'data': dict(obj.data, **{field: value})}))
    before = snapshot(runtime)
    adapter.effects.clear()
    assert session(runtime).read('a').decision == 'DENY'
    assert session(runtime).execute(TransitionState('a', 'finish'), expected_version=1).decision == 'DENY'
    for entry in ('auth', 'query', 'traverse'):
        with pytest.raises(AuthorizationContextError, match='^authorization context unavailable$'):
            caller = session(runtime)
            if entry == 'auth':
                caller.auth
            elif entry == 'query':
                caller.query(object_type='Signal')
            else:
                caller.traverse('a', max_depth=0)
    assert snapshot(runtime) == before
    assert adapter.effects == []


@pytest.mark.parametrize('malformation', ['status', 'missing-delegate', 'multiple-delegates',
                                          'multiple-principals', 'conflicting-parent', 'interval', 'parallel-scope'])
def test_malformed_preexisting_representation_does_not_select_defaults(rt, malformation):
    runtime, adapter = rt
    assert create(runtime).decision == 'ALLOW'
    obj = runtime._store.get('p')
    if malformation == 'status':
        runtime._store.save(obj.model_copy(update={'state': 'suspended'}))
    elif malformation == 'missing-delegate':
        runtime._store.remove('p', 'delegated_to', DELEGATE.id)
    elif malformation in ('multiple-delegates', 'multiple-principals'):
        relation = 'delegated_to' if malformation == 'multiple-delegates' else 'delegated_by'
        runtime._store.add(canonical_relationship('p', relation, 'another'))
    elif malformation == 'conflicting-parent':
        runtime._store.save(obj.model_copy(update={'data': dict(obj.data, parent_mandate_id='one')}))
        runtime._store.add(canonical_relationship('p', 'derived_from', 'two'))
    elif malformation == 'parallel-scope':
        runtime._store.save(obj.model_copy(update={'data': dict(obj.data, scope={'object_ids': ['a']})}))
        runtime._store.add(canonical_relationship('p', 'scoped_to', 'b'))
    else:
        runtime._store.save(obj.model_copy(update={'data': dict(obj.data,
            valid_from=(NOW+timedelta(days=1)).isoformat(), valid_until=NOW.isoformat())}))
    before = snapshot(runtime)
    adapter.effects.clear()
    assert session(runtime).execute(TransitionState('a', 'finish'), expected_version=1).decision == 'DENY'
    with pytest.raises(AuthorizationContextError):
        session(runtime).query(object_type='Signal')
    assert snapshot(runtime) == before
    assert adapter.effects == []


@pytest.mark.parametrize('field,value', [('application_id', []), ('mandate_id', ''), ('purpose', False)])
def test_malformed_query_context_is_a_sanitized_typed_refusal(rt, field, value):
    runtime, adapter = rt
    before = snapshot(runtime)
    adapter.effects.clear()
    with pytest.raises(AuthorizationContextError, match='^authorization context unavailable$'):
        runtime.query(DELEGATE, object_type='Signal', auth=AuthorizationContext(**{field: value}))
    assert snapshot(runtime) == before
    assert adapter.effects == []


def test_alternatives_within_one_declaration_and_finite_id_narrowing_remain_valid(rt):
    runtime, adapter = rt
    assert runtime.execute(AddRelationship('b', 'zone', 'z'), actor=OWNER,
                           command_context=CommandContext(expected_version=1)).decision == 'ALLOW'
    issue(runtime, scope=MandateScope(object_ids=frozenset({'a'}), object_types=frozenset({'Other'}),
          relationship_constraints=(RelationshipConstraint('zone', 'z'),)))
    for oid in ('a', 'b', 'c'):
        assert session(runtime).read(oid).decision == 'ALLOW'
    issue(runtime, 'child', principal=DELEGATE, delegate=WORKER, parent_mandate_id='p',
          scope=MandateScope(object_ids=frozenset({'a', 'c'})))
    assert session(runtime, 'child', WORKER).read('a').decision == 'ALLOW'
    assert session(runtime, 'child', WORKER).read('c').decision == 'ALLOW'
    before = snapshot(runtime)
    adapter.effects.clear()
    assert session(runtime, 'child', WORKER).execute(TransitionState('b', 'finish'), expected_version=2).decision == 'DENY'
    assert snapshot(runtime) == before
    assert adapter.effects == []


def test_empty_computed_capabilities_refuse_even_an_operation_with_no_required_capabilities(rt):
    runtime, adapter = rt
    assert create(runtime, 'p').decision == 'ALLOW'
    assert create(runtime, 'child', 'p').decision == 'ALLOW'
    for mid, caps in (('p', ['signal.read']), ('child', ['signal.finish'])):
        obj = runtime._store.get(mid)
        runtime._store.save(obj.model_copy(update={'data': dict(obj.data, capabilities=caps)}))
    before = snapshot(runtime)
    adapter.effects.clear()
    assert session(runtime, 'child').execute(CreateObject('new', 'Signal', {})).decision == 'DENY'
    assert snapshot(runtime) == before
    assert adapter.effects == []


def test_unsupported_ancestor_binding_refuses_without_retagging_descendants(rt):
    runtime, adapter = rt
    for mid, parent in (('g', None), ('p', 'g'), ('child', 'p')):
        assert create(runtime, mid, parent).decision == 'ALLOW'
    ancestor = runtime._store.get('g')
    runtime._store.save(ancestor.model_copy(update={'type_version': 'unsupported-ancestor'}))
    before = snapshot(runtime)
    adapter.effects.clear()
    assert session(runtime, 'g').read('a').decision == 'DENY'
    assert session(runtime, 'child').execute(TransitionState('a', 'finish'), expected_version=1).decision == 'DENY'
    with pytest.raises(AuthorizationContextError):
        session(runtime, 'child').query(object_type='Signal')
    assert create(runtime, 'new-child', 'p').decision == 'DENY'
    assert snapshot(runtime) == before
    assert adapter.effects == []
    assert runtime._store.get('child').type_version == '1'


def test_delegated_traversal_cannot_hydrate_or_bridge_an_inaccessible_object(rt):
    runtime, adapter = rt
    for source, target in (('a', 'b'), ('b', 'c')):
        assert runtime.execute(AddRelationship(source, 'next', target), actor=OWNER,
                               command_context=CommandContext(expected_version=1)).decision == 'ALLOW'
    issue(runtime, scope=MandateScope(object_ids=frozenset({'a', 'c'})))
    caller = session(runtime)
    before = snapshot(runtime)
    adapter.effects.clear()
    assert [node.object.id for node in caller.traverse('a', max_depth=2)] == ['a']
    assert all(oid == 'a' for _, oid in adapter.effects)
    adapter.effects.clear()
    assert runtime._query.related(DELEGATE, 'b', auth=caller.auth) == []
    assert adapter.effects == []
    assert snapshot(runtime) == before
    assert caller.read('c').decision == 'ALLOW'
