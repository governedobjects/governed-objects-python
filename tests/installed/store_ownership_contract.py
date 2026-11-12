# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Brafford Labs, LLC
"""Synthetic synchronous same-store ownership controls; public installed APIs.

No checkout discovery, native fixture, reference package or source fallback.
Raw SQLite connections and tracing are diagnostic controls only.
"""
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import sys
import threading
import time

from governed_objects.domain.models import CommandResult, Decision, GovernedObject, GovernanceEvent
from governed_objects.idempotency.store import IdempotencyStore
from governed_objects.observations.models import ExternalObservation, ObservationDecision
from governed_objects.observations.store import ObservationStore
from governed_objects.outbound.models import ProviderRepairResult, RepairStatus
from governed_objects.outbound.store import RepairStore
from governed_objects.persistence.sqlite import SQLiteStore

KIND = 'synthetic.ownership/Record'
WAIT = .1  # Intentional bounded blocking observation, not a sleep-only proof.
JOIN = 5


class Rollback(Exception):
    pass


def image(name):
    return GovernedObject(id=name, type=KIND, state='open')


def audit():
    return GovernanceEvent(event_id='audit', object_id='foreign', object_type=KIND,
                           actor_id='caller', actor_type='service', action='read',
                           decision=Decision.ALLOW, event_type='audit')


def durable(path):
    conn = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=1)
    try:
        tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
        return {t: conn.execute('SELECT * FROM "' + t + '" ORDER BY rowid').fetchall() for (t,) in tables}
    finally:
        conn.close()


class Trace:
    def __init__(self):
        self.events = []
        self.mutex = threading.Lock()

    def mark(self, kind, **facts):
        with self.mutex:
            self.events.append(dict(sequence=len(self.events), monotonic_ns=time.monotonic_ns(),
                                    thread=threading.current_thread().name, kind=kind, **facts))


class Worker:
    def __init__(self, trace, operation, name='foreign'):
        self.trace = trace
        self.entered = threading.Event()
        self.done = threading.Event()
        self.value = None
        self.error = None
        def run():
            trace.mark('method_entry')
            self.entered.set()
            try:
                self.value = operation()
                trace.mark('method_return')
            except BaseException as exc:
                self.error = exc
                trace.mark('method_error', error=type(exc).__name__)
            finally:
                self.done.set()
        self.thread = threading.Thread(target=run, name=name)
        self.thread.start()
        assert self.entered.wait(JOIN), 'worker entry timeout'

    def join(self):
        self.thread.join(JOIN)
        assert not self.thread.is_alive(), 'worker cleanup timeout'
        self.trace.mark('joined')


def operation(name, store, obs, repair, idem):
    return {
        'save': lambda: store.save(image('foreign')),
        'audit': lambda: store.append(audit()),
        'read': lambda: store.get('owned'),
        'observation': lambda: obs.save_observation(ExternalObservation(id='claim', object_id='foreign',
            provider_name='synthetic', external_id='external'), decision=ObservationDecision.NO_CHANGE),
        'review': lambda: obs.create_review_case(case_id='review', object_id='foreign', observation_id='claim',
            field_name='x', canonical_value=1, observed_value=2),
        'review-update': lambda: obs.resolve_review_case('existing', resolution='KEEP_CANONICAL', resolved_by='caller'),
        'repair': lambda: repair.save_repair(ProviderRepairResult(repair_id='repair', object_id='foreign',
            provider_name='synthetic', status=RepairStatus.PLANNED)),
        'repair-update': lambda: repair.save_repair(ProviderRepairResult(repair_id='existing', object_id='foreign',
            provider_name='synthetic', status=RepairStatus.APPLIED)),
        'sync': lambda: repair.append_sync_event(object_id='foreign', event_type='synthetic'),
        'idempotency': lambda: idem.put('foreign', 'key', CommandResult(decision=Decision.ALLOW)),
        'attempt': lambda: idem.record_attempt(command_id='command', object_id='foreign', idempotency_key='key',
            correlation_id=None, status='pending'),
        'observation-setup': lambda: ObservationStore(store),
        'repair-setup': lambda: RepairStore(store),
        'idempotency-setup': lambda: IdempotencyStore(store),
        'nested': lambda: owned_save(store),
        'restore': lambda: store.restore_imported([image('restored')], [], [], {'import_id': 'import'}),
        'observation-stats': lambda: obs.stats,
        'observation-read': lambda: obs.list_for_object('owned'),
        'dedupe-read': lambda: obs.exists_dedupe_key('synthetic:external:::x'),
        'review-read': lambda: obs.get_review_case('existing'),
        'repair-stats': lambda: repair.stats(),
        'repair-read': lambda: repair.get_repair('existing'),
        'idempotency-read': lambda: idem.get('owned', 'key'),
        'dump': lambda: store.dump_tables(),
        'highwater': lambda: store.version_high_water('owned'),
        'list': lambda: store.list_all(),
        'close': lambda: store.close(),
    }[name]


def owned_save(store):
    with store.observation_transaction():
        store.save(image('foreign'))


FOREIGN = ('save', 'audit', 'read', 'observation', 'review', 'review-update', 'repair',
           'repair-update', 'sync', 'idempotency', 'attempt', 'observation-setup',
           'repair-setup', 'idempotency-setup', 'nested', 'restore', 'observation-stats',
           'observation-read', 'dedupe-read', 'review-read', 'repair-stats', 'repair-read',
           'idempotency-read', 'dump', 'highwater', 'list', 'close')
REFUSALS = ('nested', 'observation-setup', 'repair-setup', 'idempotency-setup', 'close',
            'restore', 'schema', 'migration')
CASE_IDS = ([f'foreign/{name}/{outcome}' for name in FOREIGN for outcome in ('rollback', 'commit')]
            + [f'owner/{outcome}' for outcome in ('rollback', 'commit')]
            + [f'refuse/{name}' for name in REFUSALS]
            + ['open/observation', 'open/restore', 'registration/rollback', 'registration/commit',
               'restore/rollback', 'restore/commit'])


def foreign(path, name, commit, trace):
    store = SQLiteStore(path)
    obs, repair, idem = ObservationStore(store), RepairStore(store), IdempotencyStore(store)
    obs.create_review_case(case_id='existing', object_id='foreign', observation_id='old', field_name='x',
                           canonical_value=1, observed_value=2)
    repair.save_repair(ProviderRepairResult(repair_id='existing', object_id='foreign',
                                           provider_name='synthetic', status=RepairStatus.PLANNED))
    before = durable(path)
    store._conn.set_trace_callback(lambda sql: trace.mark('sql', sql=sql))
    worker = None
    try:
        try:
            with store.observation_transaction():
                store.save(image('owned'))
                trace.mark('owner_staged')
                worker = Worker(trace, operation(name, store, obs, repair, idem))
                try:
                    assert not worker.done.wait(WAIT), 'foreign operation returned during ownership'
                    assert not any(e['kind'] == 'sql' and e['thread'] == 'foreign' for e in trace.events)
                    assert durable(path) == before, 'premature durability'
                    trace.mark('bounded_blocking_observed', seconds=WAIT)
                finally:
                    trace.mark('owner_release', commit=commit)
                if not commit:
                    raise Rollback()
        except Rollback:
            pass
    finally:
        if worker:
            worker.join()
        if name != 'close':
            store._conn.set_trace_callback(None)
            store.close()
        else:
            store.close()  # Preserve safe idempotent close after the foreign close.
    assert worker.error is None, repr(worker.error)
    after = durable(path)
    assert ('owned' in [r[0] for r in after['objects']]) == commit
    if name in ('save', 'nested'):
        assert 'foreign' in [r[0] for r in after['objects']]
    if name == 'audit':
        assert [r[0] for r in after['events']] == ['audit']
    if name == 'read':
        assert (worker.value is not None) == commit
    if name == 'observation':
        assert [r[0] for r in after['observations']] == ['claim']
    if name == 'review':
        assert 'review' in [r[0] for r in after['review_cases']]
    if name == 'review-update':
        assert next(r for r in after['review_cases'] if r[0] == 'existing')[6] == 'rejected'
    if name in ('repair', 'repair-update'):
        row = next(r for r in after['provider_repairs'] if r[0] == ('repair' if name == 'repair' else 'existing'))
        assert row[3] == ('PLANNED' if name == 'repair' else 'APPLIED')
    if name == 'sync':
        assert len(after['sync_audit']) == 1
    if name in ('idempotency', 'attempt'):
        assert after['command_attempts'][0][4] == ('succeeded' if name == 'idempotency' else 'pending')
    if name == 'restore':
        assert 'restored' in [r[0] for r in after['objects']] and len(after['interchange_imports']) == 1
    if name == 'highwater':
        assert worker.value == (1 if commit else None)
    if name == 'list':
        assert [o.id for o in worker.value] == (['owned'] if commit else [])
    trace.mark('durability', before=before, after=after)
    # A separate autonomous call after release still commits normally.
    reopened = SQLiteStore(path)
    try:
        reopened.save(image('after'))
        assert 'after' in [r[0] for r in durable(path)['objects']]
    finally:
        reopened.close()


def owner(path, commit, trace):
    store = SQLiteStore(path)
    obs, repair, idem = ObservationStore(store), RepairStore(store), IdempotencyStore(store)
    before = durable(path)
    try:
        try:
            with store.observation_transaction():
                store.save(image('owned'))
                for name in ('audit', 'observation', 'review', 'repair', 'sync', 'idempotency'):
                    operation(name, store, obs, repair, idem)()
                assert store.get('owned') is not None and obs.stats['observations_received'] == 1
                assert repair.stats()['repairs_planned'] == 1 and idem.get('foreign', 'key') is not None
                assert durable(path) == before, 'owner helper committed prematurely'
                if not commit:
                    raise Rollback()
        except Rollback:
            pass
        after = durable(path)
        assert (len(after['objects']) == 1) == commit
        for table in ('events', 'observations', 'review_cases', 'provider_repairs', 'sync_audit', 'command_attempts'):
            assert len(after[table]) == int(commit), table
        store.save(image('after'))
        assert 'after' in [r[0] for r in durable(path)['objects']]
        trace.mark('durability', before=before, after=after)
    finally:
        store.close()


def refuse(path, name, trace, raw_open=False):
    store = SQLiteStore(path)
    obs, repair, idem = ObservationStore(store), RepairStore(store), IdempotencyStore(store)
    sql = []
    store._conn.set_trace_callback(sql.append)
    try:
        @contextmanager
        def outer():
            if raw_open:
                store._conn.execute('BEGIN IMMEDIATE')  # Privileged diagnostic setup only.
                try:
                    yield
                finally:
                    store._conn.rollback()
            else:
                with store.observation_transaction():
                    yield
        with outer():
            start = len(sql)
            try:
                if name == 'schema':
                    store._init_schema()
                elif name == 'migration':
                    store._migrate_schema()
                elif name == 'observation':
                    owned_save(store)
                else:
                    operation(name, store, obs, repair, idem)()
            except RuntimeError:
                pass
            else:
                raise AssertionError('unsafe ownership operation accepted')
            assert sql[start:] == [], 'refusal occurred after SQL'
        store.save(image('after'))
        assert store.get('after') is not None
        trace.mark('refusal_before_sql', operation=name, raw_open=raw_open, sql=sql)
    finally:
        store._conn.set_trace_callback(None)
        store.close()


def installation(path, commit, trace, restoring=False):
    """Pause real registration/import storage, never replace admission or SQLite."""
    store = SQLiteStore(path)
    staged, release = threading.Event(), threading.Event()
    def seam(original):
        def paused(obj):
            value = original(obj)
            trace.mark('installation_staged')
            staged.set()
            assert release.wait(JOIN), 'installation release timeout'
            if not commit:
                raise OSError('synthetic installation failure')
            return value
        return paused
    if restoring:
        store._insert_restored_object = seam(store._insert_restored_object)
        install = lambda: store.restore_imported([image('installed')], [], [], {'import_id': 'import'})
    else:
        from governed_objects.adapters.base import TypeAdapter
        from governed_objects.adapters.context import HydratedObject
        from governed_objects.applications.registry import ApplicationRegistry
        from governed_objects.observations.validation import CandidateVerdict
        from governed_objects.providers.binding import ProviderBinding
        from governed_objects.providers.registry import ProviderRegistry
        from governed_objects.runtime.runtime import GovernedObjectRuntime
        from governed_objects.types.registry import TypeRegistry
        from governed_objects.types.extensions import TypeExtensionRegistry
        from governed_objects.types.schema import ObjectTypeDefinition, StateDef
        class Adapter(TypeAdapter):
            def supports(self, kind): return kind == KIND
            def validate_create(self, definition, data): return True, [], ''
            def validate_transition(self, obj, definition, action, actor): return True, [], ''
            def validate_observation_candidate(self, **kwargs):
                assert not store._conn.in_transaction, 'validation inside SQL transaction'
                return CandidateVerdict.VALID
            def load(self, obj, context):
                assert not store._conn.in_transaction, 'provider load inside SQL transaction'
                return HydratedObject(object=obj.model_copy(update={'state': 'open', 'data': {}}))
        types = TypeRegistry()
        types.register(ObjectTypeDefinition(kind=KIND, type_version='1', initial_state='open',
                                            states={'open': StateDef()}))
        rt = GovernedObjectRuntime(store, type_registry=types, adapters=[Adapter()],
            extension_registry=TypeExtensionRegistry(), providers=ProviderRegistry(),
            application_registry=ApplicationRegistry())
        store.save = seam(store.save)
        install = lambda: rt.registration.register_with_binding('installed', KIND,
            ProviderBinding(provider_name='synthetic', external_id='external'), context=rt.context)
    owner_worker = foreign_worker = None
    try:
        owner_worker = Worker(trace, install, 'installer')
        assert staged.wait(JOIN), f'actual installation seam not reached: {owner_worker.error!r}'
        foreign_worker = Worker(trace, lambda: store.append(audit()))
        assert not foreign_worker.done.wait(WAIT), 'caller bypassed installation ownership'
        assert durable(path)['objects'] == [] and durable(path)['events'] == []
        trace.mark('bounded_blocking_observed', seconds=WAIT)
    finally:
        release.set()
        if owner_worker:
            owner_worker.join()
        if foreign_worker:
            foreign_worker.join()
        store.close()
    assert (owner_worker.error is None) == commit, repr(owner_worker.error)
    if not commit:
        assert isinstance(owner_worker.error, OSError)
    assert foreign_worker.error is None, repr(foreign_worker.error)
    rows = durable(path)
    assert ('installed' in [r[0] for r in rows['objects']]) == commit
    assert 'audit' in [r[0] for r in rows['events']]
    if not restoring:
        assert len(rows['events']) == (2 if commit else 1)
    trace.mark('durability', after=rows)


def run_case(case_id, path):
    trace = Trace()
    parts = case_id.split('/')
    if parts[0] == 'foreign':
        foreign(path, parts[1], parts[2] == 'commit', trace)
    elif parts[0] == 'owner':
        owner(path, parts[1] == 'commit', trace)
    elif parts[0] in ('refuse', 'open'):
        refuse(path, parts[1], trace, raw_open=parts[0] == 'open')
    else:
        installation(path, parts[1] == 'commit', trace, restoring=parts[0] == 'restore')
    return trace.events


def main():
    base = Path(sys.argv[1]).resolve()
    base.mkdir()
    results = {}
    for index, case_id in enumerate(CASE_IDS):
        try:
            results[case_id] = dict(passed=True, events=run_case(case_id, base/f'{index}.db'))
        except BaseException as exc:
            results[case_id] = dict(passed=False, error=type(exc).__name__, message=str(exc))
    import governed_objects.persistence.sqlite as module
    (base/'results.json').write_text(json.dumps(dict(cases=results, module_origin=module.__file__), indent=2))
    print(json.dumps({case: value['passed'] for case, value in results.items()}, indent=2))
    assert all(value['passed'] for value in results.values())


if __name__ == '__main__':
    main()
