# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Brafford Labs, LLC
"""Synthetic derived-ownership controls using actual public installed runtime APIs.

No checkout discovery, reference/native fixture imports or admission/replay copies.
Raw SQLite reads and test-owned storage seams are diagnostics only.
"""
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import sys
import threading
import time

from governed_objects.adapters.base import TypeAdapter
from governed_objects.adapters.context import HydratedObject
from governed_objects.applications.registry import ApplicationRegistry
from governed_objects.domain.command import AddRelationship, CreateObject, ReadObject, RemoveRelationship
from governed_objects.domain.models import Actor, ActorType, GovernedObject, Relationship
from governed_objects.observations.models import ExternalObservation
from governed_objects.observations.validation import CandidateVerdict
from governed_objects.persistence.sqlite import SQLiteStore
from governed_objects.providers.binding import ProviderBinding
from governed_objects.providers.registry import ProviderRegistry
from governed_objects.relationships.canonical import is_canonical
from governed_objects.relationships.sync import RelationshipSyncService
from governed_objects.runtime.runtime import GovernedObjectRuntime
from governed_objects.types.extensions import TypeExtensionRegistry
from governed_objects.types.lifecycle import LifecycleValidator
from governed_objects.types.registry import TypeRegistry
from governed_objects.types.schema import ActionDef, FieldSchema, ObjectTypeDefinition, StateDef
from governed_objects.types.validation import SchemaValidator

KIND = 'synthetic.relationship/Record'
VERSION = 'opaque:rel-1'
OID = 'object:opaque/α'
ACTOR = Actor(id='test-writer', type=ActorType.SERVICE)
WAIT, JOIN = .1, 5


class Adapter(TypeAdapter):
    def __init__(self, store):
        self.store = store
        self.view = []
        self.omitted = False
        self.loads = self.validations = 0

    def supports(self, kind): return kind == KIND
    def validate_create(self, definition, data):
        valid, checks, errors = SchemaValidator().validate(definition, data)
        return valid, checks, '; '.join(errors)
    def validate_transition(self, obj, definition, action, actor):
        valid, reason, check = LifecycleValidator().validate_transition(definition, obj.state, action)
        return valid, [check], reason
    def validate_observation_candidate(self, **kwargs):
        assert not self.store._conn.in_transaction
        self.validations += 1
        return CandidateVerdict.VALID
    def load(self, obj, context):
        assert not self.store._conn.in_transaction
        self.loads += 1
        if self.omitted:
            return HydratedObject(object=obj.model_copy(deep=True))
        return HydratedObject(object=obj.model_copy(deep=True), relationships=deepcopy(self.view))


class Reducer:
    def __init__(self): self.calls = []
    def reduce(self, current, event):
        self.calls.append((event.action, event.history_position, event.object_version))
        return GovernedObject(id=event.object_id, type=event.object_type, type_version=event.type_version,
            version=event.object_version, state=event.new_state or current.state,
            data=deepcopy(event.details.get('data', current.data if current else {})))


def build(path, *, canonical=False, create=True, portable=False):
    store = SQLiteStore(path)
    types = TypeRegistry()
    types.register(ObjectTypeDefinition(kind=KIND, type_version=VERSION, initial_state='open',
        fields={'value': FieldSchema(type='integer')}, states={'open': StateDef()},
        actions={name: ActionDef(open=True) for name in ('create', 'add_relationship', 'remove_relationship')},
        read_open=True))
    if portable:
        from governed_objects.interchange.types import build_type_definition, REQUIREMENT_KEYS
        types.register(build_type_definition(dict(type_identity=KIND, type_version=VERSION,
            state_fields={'value': 'string', 'status': 'lifecycle'},
            lifecycle={'states': ['open'], 'transitions': []}, relationship_types=['links'], actions={}),
            {key: [] for key in REQUIREMENT_KEYS}, open_keys=REQUIREMENT_KEYS))
    adapter, reducer = Adapter(store), Reducer()
    extensions = TypeExtensionRegistry(); extensions.register_reducer(KIND, reducer)
    from governed_objects.interchange.governance import TypeScopedGovernance
    rt = GovernedObjectRuntime(store, type_registry=types, adapters=[adapter], extension_registry=extensions,
        providers=ProviderRegistry(), application_registry=ApplicationRegistry(),
        governance=TypeScopedGovernance('synthetic', {(KIND, VERSION): 'synthetic/governance'}))
    if create:
        result = rt.execute(CreateObject(OID, KIND, {'value': '1' if portable else 1}, state='open',
            metadata={} if portable else ProviderBinding(provider_name='A', external_id='wire').to_metadata(),
            relationships=[{'relation': 'links', 'target_id': 'target', 'target_type': KIND}] if canonical else []), actor=ACTOR)
        assert result.decision == 'ALLOW', result.model_dump()
    return rt, store, adapter, reducer


def edge(target='target', *, source=OID, relation='links', target_type=None, metadata=None):
    return Relationship(source_id=source, relation=relation, target_id=target,
                        target_type=target_type, metadata=metadata or {})


def raw(path):
    conn = sqlite3.connect(path.as_uri()+'?mode=ro', uri=True, timeout=1)
    try:
        return conn.execute('SELECT rowid,source_id,relation,target_id,target_type,metadata_json FROM relationships ORDER BY source_id,relation,target_id').fetchall()
    finally:
        conn.close()


def state(rt, store):
    return dict(object=store.get(OID).model_dump(mode='json'), high_water=store.version_high_water(OID),
                history=[e.model_dump(mode='json') for e in rt.accepted_history(OID)])


def protected(path, ownership, mode, evidence):
    rt, store, adapter, reducer = build(path, canonical=ownership == 'canonical-create')
    sync = RelationshipSyncService(store)
    try:
        if ownership == 'canonical-add':
            assert rt.execute(AddRelationship(OID, 'links', 'target', target_type=KIND), actor=ACTOR).decision == 'ALLOW'
        elif ownership != 'canonical-create':
            metadata = {'kept': {'nested': ['exact', 7]}}
            if ownership != 'untagged': metadata['source'] = {'local': 'local', 'unknown': 'unknown-origin', 'providerB': 'provider'}[ownership]
            if ownership == 'providerB': metadata['provider'] = 'B'
            store.add(edge(target_type='existing-type', metadata=metadata))
        before, prior = raw(path), state(rt, store)
        evidence.update(before=before, canonical_before=[r.model_dump(mode='json') for r in rt.canonical_relationships(OID)], state_before=prior)
        proposals = [] if mode == 'empty' else [edge(target_type='different-type', metadata={'source': 'canonical', 'provider': 'forged', 'proposal': 8})]
        sync.sync_provider_relationships(OID, proposals, provider_name='A')
        if mode == 'repeat': sync.sync_provider_relationships(OID, proposals, provider_name='A')
        evidence.update(after=raw(path), canonical_after=[r.model_dump(mode='json') for r in rt.canonical_relationships(OID)], state_after=state(rt, store))
        # Compare before replay can mask the ownership defect.
        assert raw(path) == before, 'protected complete row changed before replay'
        assert state(rt, store) == prior
        assert len(rt.canonical_relationships(OID)) == int(ownership.startswith('canonical'))
        assert reducer.calls == [], 'unexpected replay used to mask preservation'
    finally:
        store.close()


def provider(path, name, evidence):
    rt, store, adapter, reducer = build(path)
    sync = RelationshipSyncService(store)
    prior = state(rt, store)
    try:
        if name in ('collision', 'vacancy'):
            sync.sync_provider_relationships(OID, [edge(metadata={'owner': 'A'})], provider_name='A')
            before = raw(path)
            sync.sync_provider_relationships(OID, [edge(target_type='B-type', metadata={'owner': 'B'})], provider_name='B')
            assert raw(path) == before
            if name == 'vacancy':
                sync.sync_provider_relationships(OID, [], provider_name='A')
                assert raw(path) == []
                sync.sync_provider_relationships(OID, [edge(target_type='B-type')], provider_name='B')
                assert store.list_for_source(OID)[0].metadata == {'source': 'provider', 'provider': 'B'}
        elif name in ('spoof-canonical', 'spoof-local', 'spoof-provider'):
            claimed = name.removeprefix('spoof-')
            proposal = edge(metadata={'source': claimed, 'provider': 'B', 'nested': {'x': [1]}})
            snapshot = proposal.model_dump(mode='json')
            sync.sync_provider_relationships(OID, [proposal], provider_name='A')
            got = store.list_for_source(OID)[0]
            assert got.metadata == {'source': 'provider', 'provider': 'A', 'nested': {'x': [1]}}
            assert proposal.model_dump(mode='json') == snapshot and not is_canonical(got)
        elif name in ('update', 'duplicates', 'repeat', 'disappearance', 'empty'):
            sync.sync_provider_relationships(OID, [edge('old'), edge(metadata={'revision': 1})], provider_name='A')
            proposals = [] if name == 'empty' else [edge(target_type='updated', metadata={'revision': 2})]
            if name == 'duplicates': proposals += [edge(target_type='last', metadata={'revision': 3})]
            sync.sync_provider_relationships(OID, proposals, provider_name='A')
            if name == 'repeat':
                value = [r.model_dump(mode='json') for r in store.list_for_source(OID)]
                sync.sync_provider_relationships(OID, proposals, provider_name='A')
                assert [r.model_dump(mode='json') for r in store.list_for_source(OID)] == value
            got = store.list_for_source(OID)
            assert len(got) == (0 if name == 'empty' else 1)
            if got:
                assert got[0].target_id == 'target' and got[0].target_type == ('last' if name == 'duplicates' else 'updated')
                assert got[0].metadata['revision'] == (3 if name == 'duplicates' else 2)
        elif name in ('blank', 'absent'):
            sync.sync_provider_relationships(OID, [edge('old')], provider_name='A')
            before = raw(path)
            sync.sync_provider_relationships(OID, [edge('new')], provider_name='' if name == 'blank' else None)
            assert raw(path) == before
        elif name == 'incoming':
            incoming = edge(OID, source='incoming-source', metadata={'source': 'provider', 'provider': 'A', 'exact': 4})
            store.add(incoming); before = raw(path)
            sync.sync_provider_relationships(OID, [incoming.model_copy(update={'target_type': 'wrong'}), edge('other', source='third-source')], provider_name='A')
            sync.sync_provider_relationships(OID, [], provider_name='A')
            assert raw(path) == before
        elif name == 'self-loop':
            sync.sync_provider_relationships(OID, [edge(OID)], provider_name='A')
            assert len(raw(path)) == 1
            sync.sync_provider_relationships(OID, [], provider_name='A')
            assert raw(path) == []
        elif name == 'protected-self-loop':
            assert rt.execute(AddRelationship(OID, 'links', OID), actor=ACTOR).decision == 'ALLOW'
            prior = state(rt, store); before = raw(path)
            sync.sync_provider_relationships(OID, [edge(OID, metadata={'forged': 'value'})], provider_name='A')
            sync.sync_provider_relationships(OID, [], provider_name='A')
            assert raw(path) == before
        elif name == 'exact-triples':
            proposals = [edge('c', relation='a|b'), edge('b|c', relation='a'), edge('β:target/opaque', relation='links'), edge('c', source='other')]
            sync.sync_provider_relationships(OID, proposals, provider_name='A')
            assert {(r.source_id, r.relation, r.target_id) for r in store.list_for_source(OID)} == {(OID, r.relation, r.target_id) for r in proposals[:3]}
        assert state(rt, store) == prior
        evidence.update(rows=raw(path), state=prior, canonical=[r.model_dump(mode='json') for r in rt.canonical_relationships(OID)])
    finally:
        store.close()


def caller(path, name, evidence):
    registration = name.startswith('registration')
    rt, store, adapter, reducer = build(path, canonical=not registration, create=not registration)
    sync = RelationshipSyncService(store)
    try:
        if registration:
            # Incoming existing cache slot owned elsewhere is never a new object's namespace.
            store.add(edge(OID, source='incoming', metadata={'source': 'local', 'keep': 1}))
            before = raw(path)
            adapter.view = [edge('derived')]
            if name == 'registration-fault':
                adapter.view = [edge('first'), edge('second')]
                original = store.add
                def fail(rel):
                    original(rel)
                    if rel.target_id == 'second': raise OSError('synthetic installation failure')
                store.add = fail
                try: rt.registration.register_with_binding(OID, KIND, ProviderBinding(provider_name='A', external_id='wire'), context=rt.context)
                except OSError: pass
                else: raise AssertionError('joined installation fault swallowed')
                assert store.get(OID) is None and rt.accepted_history(OID) == [] and raw(path) == before
                assert not store._observation_transaction_active and not store._conn.in_transaction
            else:
                obj = rt.registration.register_with_binding(OID, KIND, ProviderBinding(provider_name='A', external_id='wire'), context=rt.context)
                assert obj.version == store.version_high_water(OID) == 1
                assert [(e.action, e.history_position) for e in rt.accepted_history(OID)] == [('create', 1)]
                assert [(r.relation, r.target_id) for r in store.list_for_source(OID)] == [('links', 'derived')]
                assert rt.canonical_relationships(OID) == [] and adapter.loads == adapter.validations == 1
                assert raw(path)[0] == before[0]
        else:
            prior, before = state(rt, store), raw(path)
            adapter.view = [edge(metadata={'source': 'canonical', 'spoof': 1}), edge('derived')]
            def observe(label):
                return rt.ingest_observation(ExternalObservation(object_id=OID, provider_name='A', external_id='wire', provider_event_id=label))
            result = observe('nonempty')
            assert result.decision == 'NO_CHANGE' and adapter.loads == 1
            got = next(row for row in raw(path) if row[3] == 'target')
            assert got == before[0], 'actual observation overwrote canonical before replay'
            assert state(rt, store)['history'] == prior['history'] and store.version_high_water(OID) == prior['high_water']
            if name in ('observation-empty', 'observation-omitted'):
                cached = raw(path); adapter.view = []
                adapter.omitted = name == 'observation-omitted'
                assert observe('empty').decision == 'NO_CHANGE'
                assert raw(path) == cached, 'automatic empty hydration behavior broadened'
                sync.sync_provider_relationships(OID, [], provider_name='A')
                assert raw(path) == before, 'direct empty did not clear only derived cache'
            assert adapter.validations == 0
        evidence.update(rows=raw(path), history=[e.model_dump(mode='json') for e in rt.accepted_history(OID)], loads=adapter.loads, validations=adapter.validations)
    finally:
        store.close()


def commands(path, name, evidence):
    rt, store, adapter, reducer = build(path, portable=name == 'export')
    sync = RelationshipSyncService(store)
    try:
        if name == 'local-promotion':
            sync.add_local_relationship(edge(metadata={'kept': 'local'}))
        else:
            sync.sync_provider_relationships(OID, [edge(metadata={'source': 'canonical'})], provider_name='A')
        assert rt.canonical_relationships(OID) == []
        promoted = rt.execute(AddRelationship(OID, 'links', 'target', target_type=KIND), actor=ACTOR)
        assert promoted.decision == 'ALLOW' and promoted.object_version == 2
        noop = rt.execute(AddRelationship(OID, 'links', 'target'), actor=ACTOR)
        assert noop.object_version == 2 and len(rt.accepted_history(OID)) == 2
        prior, before = state(rt, store), raw(path)
        sync.sync_provider_relationships(OID, [edge(target_type='spoof'), edge('derived-only')], provider_name='A')
        assert next(r for r in raw(path) if r[3] == 'target') == before[0]
        assert state(rt, store) == prior
        if name == 'remove':
            removed = rt.execute(RemoveRelationship(OID, 'links', 'target'), actor=ACTOR)
            assert removed.object_version == 3
            again = rt.execute(RemoveRelationship(OID, 'links', 'target'), actor=ACTOR)
            assert again.object_version == 3 and len(rt.accepted_history(OID)) == 3
            sync.sync_provider_relationships(OID, [edge()], provider_name='A')
            assert rt.canonical_relationships(OID) == [] and store.version_high_water(OID) == 3
        elif name == 'export':
            bundle = rt.export_interchange_bundle([OID], actor=ACTOR, closed=False)
            evidence['bundle'] = bundle
            assert [(r['type'], r['target']) for r in bundle['objects'][0]['relationships']] == [('links', 'target')]
            assert rt.execute(ReadObject(OID), actor=ACTOR).decision == 'ALLOW'
        else:
            store.remove(OID, 'links', 'target')
            cached = store.get(OID); store.save(cached.model_copy(update={'data': {'value': 999}}))
            restored = rt.replay(OID)
            assert restored.data == {'value': 1} and restored.version == 2 and len(reducer.calls) == 2
            assert [(r.relation, r.target_id) for r in rt.canonical_relationships(OID)] == [('links', 'target')]
            assert state(rt, store) == prior and store.get(OID).data == {'value': 1}
        evidence.update(rows=raw(path), state=state(rt, store), reducer_calls=reducer.calls)
    finally:
        store.close()


def atomic(path, name, evidence):
    rt, store, adapter, reducer = build(path, canonical=True)
    sync = RelationshipSyncService(store)
    try:
        sync.sync_provider_relationships(OID, [edge('old')], provider_name='A')
        store.add(edge(OID, source='incoming', metadata={'source': 'provider', 'provider': 'A'}))
        before, prior = raw(path), state(rt, store)
        if name == 'fault':
            original = store.add
            def fail(rel):
                original(rel)
                if rel.target_id == 'second': raise OSError('synthetic batch failure')
            store.add = fail
            try: sync.sync_provider_relationships(OID, [edge('first'), edge('second')], provider_name='A')
            except OSError: pass
            else: raise AssertionError('batch fault swallowed')
            assert raw(path) == before and state(rt, store) == prior
        else:
            class Rollback(Exception): pass
            try:
                with store.observation_transaction():
                    sync.sync_provider_relationships(OID, [edge('new')], provider_name='A')
                    assert raw(path) == before, 'joined sync committed outer unit'
                    assert {r.target_id for r in store.list_for_source(OID)} == {'target', 'new'}
                    if name == 'joined-rollback': raise Rollback()
            except Rollback: pass
            assert (raw(path) == before) == (name == 'joined-rollback')
        assert not store._observation_transaction_active and not store._conn.in_transaction
        evidence.update(before=before, after=raw(path), state=state(rt, store))
    finally:
        store.close()


def race(path, name, evidence):
    rt, store, adapter, reducer = build(path)
    sync = RelationshipSyncService(store)
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    events = []; mutex = threading.Lock(); workers = []; errors = []
    def mark(kind):
        with mutex: events.append(dict(sequence=len(events), kind=kind, thread=threading.current_thread().name, monotonic_ns=time.monotonic_ns()))
    def worker(label, action):
        def run():
            mark(label+'-entry'); entered.set()
            try: action(); mark(label+'-return')
            except BaseException as exc: errors.append(repr(exc))
            finally: finished.set()
        t=threading.Thread(target=run, name=label);workers.append(t);t.start();return t
    original = store.list_for_source
    try:
        if name == 'promotion-first':
            lookup = threading.Event()
            def observed(oid):
                if threading.current_thread().name == 'sync': lookup.set();mark('sync-lookup')
                return original(oid)
            store.list_for_source = observed
            with store._connection_guard:
                worker('sync', lambda: sync.sync_provider_relationships(OID, [edge()], provider_name='A'))
                assert entered.wait(JOIN)
                assert not lookup.wait(WAIT), 'ownership lookup occurred before shared guard'
                assert rt.execute(AddRelationship(OID, 'links', 'target'), actor=ACTOR).object_version == 2
                mark('promotion-complete')
        else:
            staged = threading.Event()
            def paused(oid):
                value = original(oid)
                if threading.current_thread().name == 'sync':
                    mark('sync-snapshot');staged.set();assert release.wait(JOIN)
                return value
            store.list_for_source = paused
            worker('sync', lambda: sync.sync_provider_relationships(OID, [edge()], provider_name='A'))
            assert staged.wait(JOIN)
            entered.clear();finished.clear()
            def promote():
                result = rt.execute(AddRelationship(OID, 'links', 'target'), actor=ACTOR)
                assert result.decision == 'ALLOW' and result.object_version == 2
            mutation = worker('canonical', promote)
            assert entered.wait(JOIN)
            assert not finished.wait(WAIT), 'canonical mutation bypassed sync ownership'
            mark('bounded-blocking-observed');release.set()
    finally:
        release.set()
        for t in workers:
            t.join(JOIN);assert not t.is_alive(), 'worker cleanup timeout'
            mark('joined')
        store.list_for_source = original
        if sys.exc_info()[0] is not None:
            store.close()
    try:
        assert not errors, errors
        assert [(r.relation,r.target_id) for r in rt.canonical_relationships(OID)] == [('links','target')]
        assert store.version_high_water(OID) == 2 and len(rt.accepted_history(OID)) == 2
        evidence.update(events=events, rows=raw(path), state=state(rt, store), all_workers_joined=True)
    finally:
        store.close()


OWNERS = ('canonical-create', 'canonical-add', 'local', 'untagged', 'unknown', 'providerB')
PROVIDERS = ('collision', 'vacancy', 'spoof-canonical', 'spoof-local', 'spoof-provider', 'update',
             'duplicates', 'repeat', 'disappearance', 'empty', 'blank', 'absent', 'incoming',
             'self-loop', 'protected-self-loop', 'exact-triples')
CASE_IDS = ([f'protected/{owner}/{mode}' for owner in OWNERS for mode in ('collision', 'empty', 'repeat')]
    + [f'provider/{name}' for name in PROVIDERS]
    + [f'caller/{name}' for name in ('observation', 'observation-empty', 'observation-omitted', 'registration', 'registration-fault')]
    + [f'commands/{name}' for name in ('remove', 'export', 'replay', 'local-promotion')]
    + [f'atomic/{name}' for name in ('fault', 'joined-rollback', 'joined-commit')]
    + ['race/promotion-first', 'race/snapshot-first'])


def run_case(case_id, path, evidence=None):
    evidence = evidence if evidence is not None else {}
    parts = case_id.split('/')
    if parts[0] == 'protected': protected(path, parts[1], parts[2], evidence)
    else: globals()[{'provider':'provider','caller':'caller','commands':'commands','atomic':'atomic','race':'race'}[parts[0]]](path, parts[1], evidence)
    return evidence


def main(output):
    root=Path(output).resolve();root.mkdir()
    results={}
    for index,case in enumerate(CASE_IDS):
        evidence={}
        try: run_case(case,root/f'{index}.db',evidence)
        except Exception as exc:results[case]=dict(passed=False,error=type(exc).__name__,message=str(exc),evidence=evidence)
        else:results[case]=dict(passed=True,evidence=evidence)
    (root/'results.json').write_text(json.dumps(results,indent=2))
    print(json.dumps({k:v['passed'] for k,v in results.items()},indent=2))
    return 0 if all(v['passed'] for v in results.values()) else 1


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1]))
