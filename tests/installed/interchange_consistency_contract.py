# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Brafford Labs, LLC
"""Synthetic interchange consistency controls using public installed runtime APIs."""
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
import json
from pathlib import Path
import sqlite3
import sys

from governed_objects.applications.registry import ApplicationRegistry
from governed_objects.domain.command import AddRelationship, CreateObject
from governed_objects.domain.models import Actor, ActorType
from governed_objects.interchange.artifact import decode_artifact
from governed_objects.interchange.export import ExportRefused, InterchangeExporter
from governed_objects.interchange.governance import TypeScopedGovernance
from governed_objects.interchange.importer import ImportRefused, native_accepted_at
from governed_objects.interchange.types import build_type_definition, PortableTypeAdapter, REQUIREMENT_KEYS
from governed_objects.persistence.sqlite import SQLiteStore
from governed_objects.providers.registry import ProviderRegistry
from governed_objects.runtime.runtime import GovernedObjectRuntime
from governed_objects.types.extensions import TypeExtensionRegistry
from governed_objects.types.registry import TypeRegistry

KIND, VERSION = 'synthetic.consistency/Record', 'opaque:1'
ACTOR = Actor(id='writer', type=ActorType.SERVICE, capabilities=['interchange.import'])
DENIED = Actor(id='denied', type=ActorType.SERVICE)


def build(path):
    types = TypeRegistry()
    requirements = {k: [] for k in REQUIREMENT_KEYS}
    requirements['read'] = ['synthetic.read']
    open_keys = [k for k in REQUIREMENT_KEYS if k != 'read']
    types.register(build_type_definition(dict(type_identity=KIND, type_version=VERSION,
        state_fields={'value': 'string'}, relationship_types=['links'], actions={}), requirements,
        open_keys=open_keys))
    store = SQLiteStore(path)
    rt = GovernedObjectRuntime(store, type_registry=types, extension_registry=TypeExtensionRegistry(),
        adapters=[PortableTypeAdapter(types)], providers=ProviderRegistry(), application_registry=ApplicationRegistry(),
        governance=TypeScopedGovernance('synthetic', {(KIND, VERSION): 'synthetic/governance'}))
    return rt, store


def actor():
    return ACTOR.model_copy(update={'capabilities': ACTOR.capabilities + ['synthetic.read']})


def create(rt, identity):
    result = rt.execute(CreateObject(identity, KIND, {'value': 'ok'}), actor=actor())
    assert result.decision == 'ALLOW', result


def snapshot(path):
    with sqlite3.connect(path) as conn:
        names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        return {name: conn.execute('SELECT * FROM "'+name.replace('"', '""')+'" ORDER BY rowid').fetchall() for name in names}


def refused(call, family, code, identity, position=None):
    try:
        call()
    except family as exc:
        assert exc.code == code, (exc.code, code)
        assert exc.identity == identity, (exc.identity, identity)
        if family is ImportRefused:
            assert exc.position == position, (exc.position, position)
        return dict(code=exc.code, message=exc.message, identity=exc.identity, position=position)
    raise AssertionError('expected refusal')


TARGETS = ('present', 'absent', 'denied', 'nonportable', 'unsupported', 'provider')
PATTERNS = ('adjacent', 'nonadjacent', 'multiple')
BAD_EFFECTS = [[], {}, None, True, 1, 1.5, 'unknown']
TIMES = {
    'offset-plus': ('2026-01-01T00:00:00+24:00', 'MALFORMED_ARTIFACT'),
    'offset-minus': ('2026-01-01T00:00:00-99:99', 'MALFORMED_ARTIFACT'),
    'calendar': ('2026-02-30T00:00:00Z', 'MALFORMED_ARTIFACT'),
    'utc-underflow': ('0001-01-01T00:00:00+01:00', 'MALFORMED_ARTIFACT'),
    'utc-overflow': ('9999-12-31T23:59:59-01:00', 'MALFORMED_ARTIFACT'),
    'syntax': ('not-time', 'MALFORMED_ARTIFACT'),
    'nonstring': ([], 'MALFORMED_ARTIFACT'),
    'precision': ('2026-01-01T00:00:00.1234567Z', 'UNSUPPORTED_SEMANTIC_FEATURE'),
    'marker': ('0001-01-01T00:00:00Z', 'UNSUPPORTED_SEMANTIC_FEATURE'),
    'offset-before-precision': ('2026-01-01T00:00:00.1234567+24:00', 'MALFORMED_ARTIFACT'),
    'precision-before-calendar': ('2026-02-30T00:00:00.1234567Z', 'UNSUPPORTED_SEMANTIC_FEATURE'),
}
VALID_TIMES = {
    'utc': ('2026-01-01T00:00:00Z', datetime(2026, 1, 1, tzinfo=UTC)),
    'plus': ('2026-01-01T01:30:00+01:30', datetime(2026, 1, 1, tzinfo=UTC)),
    'minus': ('2025-12-31T22:30:00-01:30', datetime(2026, 1, 1, tzinfo=UTC)),
    'lowercase': ('2026-01-01t00:00:00z', datetime(2026, 1, 1, tzinfo=UTC)),
    'exact': ('2026-01-01T00:00:00.123456Z', datetime(2026, 1, 1, microsecond=123456, tzinfo=UTC)),
    'zero-tail': ('2026-01-01T00:00:00.123456000Z', datetime(2026, 1, 1, microsecond=123456, tzinfo=UTC)),
    'minute-normalization': ('2026-01-01T01:00:00+00:60', datetime(2026, 1, 1, tzinfo=UTC)),
}
CASE_IDS = tuple(
    [f'duplicate/{t}/{p}/{int(c)}/{int(s)}' for t in TARGETS for p in PATTERNS for c in (False, True) for s in (False, True)]
    + [f'effect/{i}' for i in range(len(BAD_EFFECTS))]
    + [f'time/{n}' for n in TIMES] + [f'valid-time/{n}' for n in VALID_TIMES]
    + [f'payload/{n}' for n in ('create', 'state_change', 'lifecycle_transition', 'relationship_add', 'relationship_remove')]
    + [f'shape/{n}' for n in ('missing-effect', 'missing-content', 'extra-content', 'extra-member', 'bad-position', 'bad-evidence')]
    + ['unique-order', 'selection-snapshot', 'empty', 'preauthorization', 'store-fault', 'native-nonstring', 'sparse-continuation']
)


def run_case(case, path, evidence):
    rt, store = build(path)
    receiver_path = path.with_name(path.stem+'-receiver.db')
    receiver, target = build(receiver_path)
    try:
        if case.startswith('duplicate/'):
            _, kind, pattern, closed, stamps = case.split('/')
            identity = 'absent' if kind == 'absent' else 'one'
            if kind != 'absent':
                create(rt, identity)
                obj = store.get(identity)
                if kind == 'nonportable': obj.type = 'not-portable'
                if kind == 'unsupported': obj.type_version = 'unsupported'
                if kind == 'provider': obj.metadata['provider_binding'] = {'provider_name': 'fake'}
                store.save(obj)
            selection = {'adjacent': [identity, identity], 'nonadjacent': [identity, 'other', identity],
                         'multiple': [identity, identity, 'other', 'other']}[pattern]
            before = snapshot(path)
            calls = []
            def forbidden(*args, **kwargs):
                calls.append('unexpected work')
                raise AssertionError('duplicate reached lookup/callback')
            exporter = InterchangeExporter(rt, store, rt._types, rt._policy)
            rt.get_object = rt.accepted_history = rt.history = rt.canonical_relationships = forbidden
            rt._policy.evaluate = forbidden
            exporter._export_object = forbidden
            rt.clock.now = forbidden
            prior = list(selection)
            result = refused(lambda: exporter.export(selection, actor=DENIED if kind == 'denied' else actor(),
                closed=bool(int(closed)), include_timestamps=bool(int(stamps))), ExportRefused,
                'DUPLICATE_IDENTITY_IN_BUNDLE', None)
            assert result['message'] == 'export selection contains repeated identities'
            assert selection == prior and not calls and snapshot(path) == before
            evidence.update(refusal=result, calls=calls, source_unchanged=True)
            return
        create(rt, 'one')
        doc = rt.export_interchange_bundle(['one'], actor=actor(), closed=True)
        before = snapshot(receiver_path)
        if case.startswith('effect/'):
            doc['objects'][0]['history'][0]['effect'] = BAD_EFFECTS[int(case.split('/')[1])]
            result = refused(lambda: receiver.import_interchange_bundle(decode_artifact(json.dumps(doc)), actor=actor()),
                ImportRefused, 'MALFORMED_ARTIFACT', 'one', 1)
            evidence['refusal'] = result
        elif case.startswith('payload/'):
            effect = case.split('/')[1]
            entry = doc['objects'][0]['history'][0]
            entry['effect'] = effect
            if effect != 'create':
                del entry['relationships']
            if effect.startswith('relationship_'):
                del entry['post_image']
                entry['relationship'] = {'source': 'one', 'type': 'links', 'target': 'one'}
            bundle = decode_artifact(json.dumps(doc))
            assert bundle.objects[0].history[0].effect == effect
            evidence['decoded_effect'] = effect
        elif case.startswith('shape/'):
            name = case.split('/')[1]
            entry = doc['objects'][0]['history'][0]
            position = 1
            if name == 'missing-effect': del entry['effect']; position = None
            if name == 'missing-content': del entry['post_image']
            if name == 'extra-content': entry['relationship'] = {'source': 'one', 'type': 'links', 'target': 'one'}
            if name == 'extra-member': entry['unknown'] = 1
            if name == 'bad-position': entry['position'] = True; position = None
            if name == 'bad-evidence': entry['actor'] = False
            evidence['refusal'] = refused(lambda: receiver.import_interchange_bundle(decode_artifact(json.dumps(doc)), actor=actor()),
                ImportRefused, 'MALFORMED_ARTIFACT', 'one', position)
        elif case.startswith('time/'):
            text, code = TIMES[case.split('/')[1]]
            refused(lambda: native_accepted_at(text, 'one', 1), ImportRefused, code, 'one', 1)
            doc['objects'][0]['history'][0]['accepted_at'] = text
            evidence['refusal'] = refused(lambda: receiver.import_interchange_bundle(decode_artifact(json.dumps(doc)), actor=actor()),
                ImportRefused, 'MALFORMED_ARTIFACT' if not isinstance(text, str) else code, 'one', 1)
        elif case.startswith('valid-time/'):
            text, expected = VALID_TIMES[case.split('/')[1]]
            assert native_accepted_at(text, 'one', 1) == expected
            doc['objects'][0]['history'][0]['accepted_at'] = text
            receiver.import_interchange_bundle(decode_artifact(json.dumps(doc)), actor=actor())
            assert receiver.accepted_history('one')[0].timestamp == expected
            out = receiver.export_interchange_bundle(['one'], actor=actor(), closed=True, include_timestamps=True)
            assert native_accepted_at(out['objects'][0]['history'][0]['accepted_at'], 'one', 1) == expected
            evidence['instant'] = expected.isoformat()
            return
        elif case in ('unique-order', 'selection-snapshot'):
            identities = ['one', 'One', ' one', 'one ', 'a|b', 'a', 'object:\u03b1']
            for identity in identities[1:]: create(rt, identity)
            requested = list(identities)
            if case == 'selection-snapshot':
                original = rt._policy.evaluate
                def policy(*args, **kwargs):
                    requested[:] = ['absent', 'absent']
                    return original(*args, **kwargs)
                rt._policy.evaluate = policy
            out = rt.export_interchange_bundle(requested, actor=actor(), closed=False, include_timestamps=True)
            assert out['manifest']['identities'] == identities and out['manifest']['closed'] is False
            receiver.import_interchange_bundle(decode_artifact(json.dumps(out)), actor=actor())
            assert all(target.get(identity).data == {'value': 'ok'} for identity in identities)
            evidence['order'] = identities
            return
        elif case == 'empty':
            out = rt.export_interchange_bundle([], actor=actor(), closed=True)
            assert out['objects'] == [] and out['manifest']['identities'] == []
            refused(lambda: receiver.import_interchange_bundle(decode_artifact(json.dumps(out)), actor=actor()),
                ImportRefused, 'MALFORMED_ARTIFACT', None)
        elif case == 'preauthorization':
            create(rt, 'other')
            obj = store.get('one'); obj.metadata['provider_binding'] = {'provider_name': 'fake'}; store.save(obj)
            exporter = InterchangeExporter(rt, store, rt._types, rt._policy)
            calls = []
            original = rt._policy.evaluate
            def policy(*args, **kwargs):
                calls.append(args[2].id)
                selected_actor = actor() if args[2].id == 'one' else DENIED
                return original(selected_actor, *args[1:], **kwargs)
            rt._policy.evaluate = policy
            exporter._export_object = lambda *args: (_ for _ in ()).throw(AssertionError('classified before authorization'))
            refused(lambda: exporter.export(['one', 'other'], actor=actor(), closed=True), ExportRefused, 'AUTHORIZATION_DENIED', 'other')
            assert calls == ['one', 'other']
        elif case == 'store-fault':
            def fault(*args, **kwargs): raise RuntimeError('injected storage fault')
            target.restore_imported = fault
            try: receiver.import_interchange_bundle(decode_artifact(json.dumps(doc)), actor=actor())
            except RuntimeError as exc: assert str(exc) == 'injected storage fault'
            else: raise AssertionError('store fault swallowed')
        elif case == 'native-nonstring':
            bundle = decode_artifact(json.dumps(doc))
            obj = bundle.objects[0]
            entry = obj.history[0]
            entry = replace(entry, evidence=replace(entry.evidence, accepted_at=1))
            bundle = replace(bundle, objects=(replace(obj, history=(entry,)),))
            evidence['refusal'] = refused(lambda: receiver.import_interchange_bundle(bundle, actor=actor()),
                ImportRefused, 'MALFORMED_ARTIFACT', 'one', 1)
        elif case == 'sparse-continuation':
            obj = doc['objects'][0]
            first = obj['history'][0]
            first['resulting_object_version'] = '40'
            obj['history'] += [dict(position=2, actor='historical', effect='relationship_add', type_identity=KIND, type_version=VERSION,
                resulting_object_version='98', relationship={'source': 'one', 'type': 'links', 'target': 'one'}),
                dict(position=3, actor='historical', effect='relationship_remove', type_identity=KIND, type_version=VERSION,
                resulting_object_version='3', relationship={'source': 'one', 'type': 'links', 'target': 'one'})]
            obj['current_object_version'] = '3'
            receiver.import_interchange_bundle(decode_artifact(json.dumps(doc)), actor=actor())
            assert [e.object_version for e in receiver.accepted_history('one')] == [40, 98, 3]
            assert target.version_high_water('one') == 98
            result = receiver.execute(AddRelationship('one', 'links', 'one'), actor=actor())
            assert result.decision == 'ALLOW', result
            assert target.get('one').version == 99 and target.version_high_water('one') == 99
            assert [e.history_position for e in receiver.accepted_history('one')] == [1, 2, 3, 4]
            out = receiver.export_interchange_bundle(['one'], actor=actor(), closed=True)
            assert [e['resulting_object_version'] for e in out['objects'][0]['history']] == ['40', '98', '3', '99']
            assert decode_artifact(json.dumps(out)).objects[0].current_object_version == '99'
            evidence['tokens'] = ['40', '98', '3', '99']
            return
        else:
            raise AssertionError(case)
        assert snapshot(receiver_path) == before, 'receiver table effects on refusal'
        evidence['receiver_tables_unchanged'] = sorted(before)
    finally:
        target.close()
        store.close()


def main(output):
    base = Path(output); base.mkdir()
    results = {}
    for i, case in enumerate(CASE_IDS):
        evidence = {}
        try:
            run_case(case, base/f'{i}.db', evidence)
        except Exception as exc:
            results[case] = dict(passed=False, error=type(exc).__name__, message=str(exc), evidence=evidence)
        else:
            results[case] = dict(passed=True, evidence=evidence)
    (base/'results.json').write_text(json.dumps(results, indent=2, sort_keys=True)+'\n')
    failed = [n for n,v in results.items() if not v['passed']]
    print(json.dumps(dict(total=len(results), failed=failed)))
    return int(bool(failed))


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1]))
