# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Brafford Labs, LLC
"""Synthetic standalone exact-binding contract; public installed imports only.

No checkout discovery, native fixtures, private imports or expected-state replayer.
"""
from copy import deepcopy
from pathlib import Path
import json
import sys

from governed_objects.applications.registry import ApplicationRegistry
from governed_objects.domain.command import AddRelationship, Command, CreateObject, RemoveRelationship, TransitionState
from governed_objects.domain.command_context import CommandContext
from governed_objects.domain.models import Actor, ActorType, GovernedObject
from governed_objects.events.replay import EventReplayer, LegacyHistoryRefused
from governed_objects.interchange.export import ExportRefused, EXPORT_READ_DENIED
from governed_objects.interchange.governance import TypeScopedGovernance
from governed_objects.interchange.types import PortableTypeAdapter, build_type_definition
from governed_objects.observations.models import ExternalObservation, FieldChange
from governed_objects.persistence.sqlite import SQLiteStore
from governed_objects.providers.registry import ProviderRegistry
from governed_objects.runtime.runtime import GovernedObjectRuntime
from governed_objects.types.extensions import TypeExtensionRegistry
from governed_objects.types.registry import TypeRegistry

KIND, VERSION = 'independent.binding/Signal', 'opaque:v3/not-ordered'
REQUIREMENTS = {key: ['probe.'+key] for key in ('read','create','state','lifecycle','relationship_add','relationship_remove')}
TYPE = dict(type_identity=KIND,type_version=VERSION,state_fields={'value':'string','phase':'lifecycle'},
            lifecycle={'states':['open','closed'],'transitions':[['open','closed']]},relationship_types=['links'],
            actions={'update':{'kind':'state'},'close':{'kind':'lifecycle','from':['open'],'to':'closed'}})
WRITER = Actor(id='writer',type=ActorType.SERVICE,capabilities=[cap for caps in REQUIREMENTS.values() for cap in caps])

class Registry(TypeRegistry):
    def __bool__(self):
        return False

class Reducer:
    def __init__(self):
        self.calls=[]
        self.conflict=None
        self.conflict_at=1
        self.decline=None
        self.omit=False

    def reduce(self,obj,event):
        self.calls.append(event.history_position)
        if self.decline == len(self.calls):
            return None
        image=event.details.get('accepted_post_image')
        if image:
            state,data=image['state'],deepcopy(image['data'])
        elif obj is None:
            state,data=event.new_state,deepcopy(event.details.get('data',{}))
        else:
            state,data=event.new_state or obj.state,deepcopy(obj.data)
        result=GovernedObject(id=event.object_id,type=event.object_type,type_version=event.type_version,
                              version=event.object_version,state=state,data=data)
        if self.omit=='default':
            result=GovernedObject(**{k:v for k,v in result.model_dump().items() if k!='type_version'})
        elif self.omit:
            result=result.model_copy(update={'type_version':None})
        return result.model_copy(update=(self.conflict or {}) if len(self.calls)==self.conflict_at else {})

class Adapter(PortableTypeAdapter):
    def __init__(self,types):
        super().__init__(types)
        self.effects=[]

    def load(self,obj,context):
        self.effects.append('load')
        return super().load(obj,context)

    def adapter_execute(self,obj,command,context):
        self.effects.append('execute')
        return super().adapter_execute(obj,command,context)

def build(path):
    types=Registry();definition=build_type_definition(TYPE,REQUIREMENTS)
    definition.extra['authority']={'fields':{'value':'PROVIDER_AUTO_ACCEPT'}}
    types.register(definition)
    extensions=TypeExtensionRegistry();reducer=Reducer();extensions.register_reducer(KIND,reducer)
    store=SQLiteStore(path);adapter=Adapter(types)
    runtime=GovernedObjectRuntime(store,type_registry=types,adapters=[adapter],extension_registry=extensions,
        providers=ProviderRegistry(),application_registry=ApplicationRegistry(),
        governance=TypeScopedGovernance('probe',{(KIND,VERSION):'independent/governance'}))
    assert runtime.execute(CreateObject('signal',KIND,{'value':'first'},state='open'),actor=WRITER).decision=='ALLOW'
    assert runtime.execute(Command('update','signal',payload={'set':{'value':'accepted'}}),actor=WRITER).decision=='ALLOW'
    assert runtime.execute(AddRelationship('signal','links','target'),actor=WRITER).decision=='ALLOW'
    adapter.effects.clear();reducer.calls.clear()
    return runtime,types,reducer,adapter

def snapshot(rt):
    return deepcopy(rt._store.dump_tables())

def unsupported(types,mode):
    if mode=='replacement':
        types.register(types.get(KIND).model_copy(update={'type_version':'replacement'}))
    elif mode=='unknown':
        types._types.pop(KIND)
    elif mode in ('permissive','strict'):
        original=types.get(KIND)
        requirements=[] if mode=='permissive' else ['replacement.only']
        types.register(original.model_copy(update={'type_version':'replacement','read_requires':requirements,
            'actions':{name:action.model_copy(update={'requires':requirements}) for name,action in original.actions.items()}}))

def refused(call):
    try:
        call()
    except Exception as exc:
        assert type(exc).__name__=='ReplayRefused'
        assert exc.code=='REPLAY_NOT_SUPPORTED' and exc.args==('reconstruction unavailable',)
        assert vars(exc)=={},vars(exc)
    else:
        raise AssertionError('expected content-free ReplayRefused')

def replay_case(path,mode,entry):
    rt,types,reducer,adapter=build(path)
    try:
        selected=None if mode=='absent-registry' else types
        replay=EventReplayer(rt._store,rt._extensions,selected)
        assert replay._types is selected
        unsupported(types,mode)
        before=snapshot(rt)
        call=(lambda:rt.replay('signal')) if entry=='wrapper' else (lambda:getattr(replay,entry)('signal'))
        if mode=='absent-registry' and entry=='wrapper':
            rt._replayer._types=None
        refused(call)
        assert snapshot(rt)==before and reducer.calls==[] and adapter.effects==[]
    finally:
        rt._store.close()

def history_case(path,field,value,entry,legacy=False):
    rt,types,reducer,adapter=build(path)
    try:
        rt._store._conn.execute(f'UPDATE events SET {field}=? WHERE history_position=3',(value,))
        if legacy:
            rt._store._conn.execute('UPDATE events SET history_position=NULL')
        rt._store._conn.commit();before=snapshot(rt)
        replay=EventReplayer(rt._store,rt._extensions,types)
        target=rt.replay if entry=='wrapper' else getattr(replay,entry)
        refused(lambda:target('signal',allow_legacy=legacy))
        assert snapshot(rt)==before and reducer.calls==[]
    finally:
        rt._store.close()

def result_case(path,field,position=1):
    rt,types,reducer,adapter=build(path)
    try:
        reducer.conflict={field:'conflicting'};reducer.conflict_at=position;before=snapshot(rt)
        refused(lambda:rt.replay('signal'))
        assert len(reducer.calls)==position and snapshot(rt)==before
    finally:
        rt._store.close()

def reconstruction_case(path,mode):
    rt,types,reducer,adapter=build(path)
    try:
        # Sparse/nonmonotonic Object Versions, positions are the order authority.
        for position,token in enumerate((40,7,31),1):
            rt._store._conn.execute('UPDATE events SET object_version=? WHERE history_position=?',(token,position))
        rt._store._conn.execute("UPDATE objects SET data_json='{}',state='discarded',version=999,version_high_water=999 WHERE id='signal'")
        rt._store._conn.commit();rt._store.remove('signal','links','target')
        reducer.omit=mode=='omit-result-version'
        obj=rt.replay('signal')
        assert (obj.type_version,obj.version,obj.state,obj.data)==(VERSION,31,'open',{'value':'accepted'})
        assert len(reducer.calls)==3
        assert [(r.relation,r.target_id) for r in rt.canonical_relationships('signal')]==[('links','target')]
        assert rt._store.version_high_water('signal')==999
    finally:
        rt._store.close()

def mutation_case(path,kind,mode,actor_mode):
    rt,types,reducer,adapter=build(path)
    try:
        unsupported(types,mode);before=snapshot(rt)
        def forbidden(*args,**kwargs):
            raise AssertionError('replacement policy must not run')
        rt._policy.evaluate=forbidden
        actor=WRITER if actor_mode=='writer' else Actor(id='blind',type=ActorType.SERVICE)
        commands={'transition':TransitionState('signal','close'),'generic':Command('update','signal',payload={'set':{'value':'new'}}),
                  'remove':RemoveRelationship('signal','links','target'),'noop':RemoveRelationship('signal','links','missing')}
        result=rt.execute(commands[kind],actor=actor,command_context=CommandContext(expected_version=3,idempotency_key='retry'))
        assert result.decision=='DENY' and result.reason=='access denied' and result.deny_reason=='READ_NOT_AUTHORIZED'
        assert result.data=={} and result.events==[] and result.object_version is None and result.validation_checks==[]
        assert snapshot(rt)==before and reducer.calls==[] and adapter.effects==[]
    finally:
        rt._store.close()

def observation_case(path,mode,variant):
    rt,types,reducer,adapter=build(path)
    try:
        service=rt._observations
        case_id='case'
        if variant in ('ACCEPT_EXTERNAL','KEEP_CANONICAL'):
            service.observation_store.create_review_case(case_id=case_id,object_id='signal',observation_id='original',field_name='value',canonical_value='accepted',observed_value='observed')
        unsupported(types,mode);before=snapshot(rt)
        def forbidden(*args,**kwargs):
            raise AssertionError('replacement observation interpretation/effects must not run')
        service._authority.mode_for_field=forbidden
        service._adapters.resolve=forbidden
        service._outbound.plan=forbidden
        if variant in ('ACCEPT_EXTERNAL','KEEP_CANONICAL'):
            result=rt.resolve_review(case_id,resolution=variant,resolved_by='trusted')
        else:
            obs=ExternalObservation(object_id='signal',provider_name='p',external_id='e',provider_event_id='event',
                changes={} if variant=='empty' else {'value':FieldChange(observed='observed')},raw_metadata={'bootstrap':variant=='bootstrap'})
            result=rt.ingest_observation(obs)
        assert result.decision=='REJECT' and result.reason=='TYPE_VERSION_UNSUPPORTED'
        assert result.field_results=={} and result.object_version is None and result.differences==[]
        assert snapshot(rt)==before and reducer.calls==[] and adapter.effects==[]
    finally:
        rt._store.close()

def export_case(path,mode):
    rt,types,reducer,adapter=build(path)
    try:
        unsupported(types,mode);before=snapshot(rt)
        def forbidden(*args,**kwargs):
            raise AssertionError('replacement export policy/classification must not run')
        rt._policy.evaluate=forbidden
        try:
            rt.export_interchange_bundle(['signal'],actor=WRITER,closed=False)
        except ExportRefused as exc:
            assert (exc.code,exc.message,exc.identity)==('AUTHORIZATION_DENIED',EXPORT_READ_DENIED,'signal')
        else:
            raise AssertionError('unsupported export accepted')
        assert snapshot(rt)==before and adapter.effects==[]
    finally:
        rt._store.close()

def compatibility_case(path,mode):
    rt,types,reducer,adapter=build(path)
    try:
        replayer=EventReplayer(rt._store,rt._extensions,types)
        if mode=='missing':
            assert replayer.reconstruct('absent') is None
            assert replayer.reconstruct_relationships('absent')==[]
            assert rt.replay('absent') is None
            return
        cached=rt.get_object('signal',raw=True)
        if mode=='no-history':
            rt._store._conn.execute('DELETE FROM events');rt._store._conn.commit()
        elif mode=='no-reducer':
            rt._extensions=TypeExtensionRegistry()
            replayer=EventReplayer(rt._store,rt._extensions,types)
        elif mode=='initial-decline':
            reducer.decline=1
        elif mode=='later-decline':
            reducer.decline=2
        elif mode=='legacy':
            rt._store._conn.execute('UPDATE events SET history_position=NULL');rt._store._conn.commit()
            try:
                replayer.reconstruct('signal')
            except LegacyHistoryRefused:
                pass
            else:
                raise AssertionError('expected legacy ordering refusal')
        elif mode=='empty-binding':
            rt._store._conn.execute("UPDATE objects SET type_version='' WHERE id='signal'");rt._store._conn.commit()
            before=snapshot(rt);refused(lambda:rt.replay('signal'))
            assert snapshot(rt)==before and reducer.calls==[]
            return
        elif mode=='no-implicit-version':
            # Constructor-default version1 is omitted, not authority for binding1.
            reducer.omit='default'
        result=replayer.reconstruct('signal',allow_legacy=mode=='legacy')
        assert result.type_version==VERSION and result.version==cached.version
        if mode=='later-decline':
            assert result.data=={'value':'first'} and len(reducer.calls)==3
        elif mode in ('no-history','no-reducer','initial-decline'):
            assert result.model_dump()==cached.model_dump()
        else:
            assert result.data=={'value':'accepted'}
    finally:
        rt._store.close()

def supported_case(path,mode):
    rt,types,reducer,adapter=build(path)
    try:
        if mode=='observation':
            result=rt.ingest_observation(ExternalObservation(object_id='signal',provider_name='p',external_id='e',
                provider_event_id='positive',changes={'value':FieldChange(observed='observed')}))
            assert result.decision=='ACCEPT' and rt.get_object('signal',raw=True).data['value']=='observed'
        elif mode=='bootstrap':
            result=rt.ingest_observation(ExternalObservation(object_id='signal',provider_name='p',external_id='e',
                provider_event_id='positive',changes={'value':FieldChange(observed='bootstrap')},raw_metadata={'bootstrap':True}))
            assert result.decision=='ACCEPT' and rt.get_object('signal',raw=True).data['value']=='bootstrap'
        elif mode=='review':
            rt._observations.observation_store.create_review_case(case_id='positive',object_id='signal',observation_id='o',field_name='value',canonical_value='accepted',observed_value='reviewed')
            result=rt.resolve_review('positive',resolution='ACCEPT_EXTERNAL',resolved_by='trusted')
            assert result.decision=='ACCEPT' and rt.get_object('signal',raw=True).data['value']=='reviewed'
        elif mode=='mutation':
            assert rt.execute(RemoveRelationship('signal','links','target'),actor=WRITER).decision=='ALLOW'
            assert rt.execute(TransitionState('signal','close'),actor=WRITER).decision=='ALLOW'
            assert rt.get_object('signal',raw=True).state=='closed'
        elif mode=='mixed-export':
            rt._store._conn.execute("UPDATE events SET type_version='historical' WHERE history_position=1")
            rt._store._conn.commit()
            bundle=rt.export_interchange_bundle(['signal'],actor=WRITER,closed=False)
            assert bundle['objects'][0]['history'][0]['type_version']=='historical'
    finally:
        rt._store.close()

def cases():
    result={}
    for mode in ('missing','no-history','no-reducer','initial-decline','later-decline','legacy','empty-binding','no-implicit-version'):
        result['compatibility/'+mode]=lambda p,m=mode:compatibility_case(p,m)
    for mode in ('observation','bootstrap','review','mutation','mixed-export'):
        result['supported/'+mode]=lambda p,m=mode:supported_case(p,m)
    for mode in ('absent-registry','replacement','unknown'):
        for entry in ('reconstruct','reconstruct_relationships','wrapper'):
            result[f'replay/{mode}/{entry}']=lambda p,m=mode,e=entry:replay_case(p,m,e)
    for field,value in (('object_type','other'),('type_version','other'),('type_version',None)):
        for entry in ('reconstruct','reconstruct_relationships','wrapper'):
            for legacy in (False,True):
                result[f'history/{field}/{value}/{entry}/{legacy}']=lambda p,f=field,v=value,e=entry,l=legacy:history_case(p,f,v,e,l)
    for field in ('id','type','type_version'):
        result['result/'+field]=lambda p,f=field:result_case(p,f)
        result['late-result/'+field]=lambda p,f=field:result_case(p,f,3)
    for mode in ('normal','omit-result-version'):
        result['reconstruction/'+mode]=lambda p,m=mode:reconstruction_case(p,m)
    for mode in ('replacement','unknown','permissive','strict'):
        for kind in ('transition','generic','remove','noop'):
            for actor in ('writer','blind'):
                result[f'mutation/{mode}/{kind}/{actor}']=lambda p,m=mode,k=kind,a=actor:mutation_case(p,k,m,a)
        for variant in ('ordinary','bootstrap','empty','ACCEPT_EXTERNAL','KEEP_CANONICAL'):
            result[f'observation/{mode}/{variant}']=lambda p,m=mode,v=variant:observation_case(p,m,v)
        result['export/'+mode]=lambda p,m=mode:export_case(p,m)
    return result

def main(directory):
    directory=Path(directory);directory.mkdir(exist_ok=False)
    results={}
    for index,(name,call) in enumerate(cases().items()):
        try:
            call(directory/f'{index}.db');results[name]={'pass':True}
        except Exception as exc:
            results[name]={'pass':False,'error_type':type(exc).__name__,'message':str(exc)}
    (directory/'results.json').write_text(json.dumps(results,indent=2)+'\n')
    print(json.dumps({'cases':len(results),'passed':sum(v['pass'] for v in results.values())}))
    return 0 if all(v['pass'] for v in results.values()) else 1

if __name__=='__main__':
    raise SystemExit(main(sys.argv[1]))
