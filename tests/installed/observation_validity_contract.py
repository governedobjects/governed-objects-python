# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Brafford Labs, LLC
"""Independent synthetic validity controls using the actual public runtime/store.

Standalone installed driver: no checkout discovery, native fixtures or private imports.
"""
from copy import deepcopy
from pathlib import Path
import json
import sys

from governed_objects.adapters.base import TypeAdapter
from governed_objects.adapters.context import HydratedObject
from governed_objects.applications.registry import ApplicationRegistry
from governed_objects.domain.command import CreateObject
from governed_objects.domain.models import Actor, ActorType, Relationship
from governed_objects.observations.models import ExternalObservation, FieldChange, ObservationSource
from governed_objects.persistence.sqlite import SQLiteStore, INT64_MAX
from governed_objects.providers.binding import ProviderBinding
from governed_objects.providers.registry import ProviderRegistry
from governed_objects.runtime.runtime import GovernedObjectRuntime
from governed_objects.types.extensions import TypeExtensionRegistry
from governed_objects.types.registry import TypeRegistry
from governed_objects.types.schema import ObjectTypeDefinition, FieldSchema, StateDef, ActionDef
from governed_objects.types.validation import SchemaValidator
from governed_objects.types.lifecycle import LifecycleValidator

KIND='independent.validity/Window'
VERSION='opaque:bound'
WRITER=Actor(id='writer',type=ActorType.SERVICE)

class Registry(TypeRegistry):
    def __bool__(self):return False

class Adapter(TypeAdapter):
    def __init__(self):
        self.behavior='valid';self.calls=0;self.loads=0;self.initial={'x':1,'y':3};self.initial_state='open';self.on_validate=None;self.reader=None
    def supports(self,kind):return kind==KIND
    def validate_create(self,definition,data):return SchemaValidator().validate(definition,data)
    def validate_transition(self,obj,definition,action,actor):
        valid,reason,check=LifecycleValidator().validate_transition(definition,obj.state,action)
        return valid,[check],reason
    def validate_observation_candidate(self,*,type_def,current,candidate,mode):
        from governed_objects.observations.validation import CandidateVerdict
        self.calls+=1
        assert not hasattr(candidate,'version') and not hasattr(candidate,'metadata')
        if self.on_validate:self.on_validate()
        if self.behavior=='base':return super().validate_observation_candidate(type_def=type_def,current=current,candidate=candidate,mode=mode)
        if self.behavior=='none':return None
        if self.behavior=='raw':return 'VALID'
        if self.behavior=='throw':raise RuntimeError('domain-private-detail')
        if self.behavior=='mutate':candidate.data['x']=99
        if self.behavior=='definition-mutate':type_def.fields.clear()
        if self.behavior=='unavailable':return CandidateVerdict.UNAVAILABLE
        return CandidateVerdict.VALID if candidate.data.get('x',0)<=candidate.data.get('y',0) else CandidateVerdict.INVALID
    def load(self,obj,context):
        self.loads+=1
        if self.reader is not None:return self.reader(obj,context)
        if self.initial is None:return None
        return HydratedObject(object=obj.model_copy(update={'data':deepcopy(self.initial),'state':self.initial_state}),
            relationships=[Relationship(source_id=obj.id,relation='links',target_id='target')])

class Reducer:
    def __init__(self):self.calls=0
    def reduce(self,obj,event):
        self.calls+=1
        from governed_objects.domain.models import GovernedObject
        data=deepcopy(event.details.get('data',obj.data if obj is not None else {}))
        return GovernedObject(id=event.object_id,type=event.object_type,type_version=event.type_version,
            version=event.object_version,state=event.new_state or obj.state,data=data)

def build(path,*,create=True):
    types=Registry();types.register(ObjectTypeDefinition(kind=KIND,type_version=VERSION,initial_state='open',
        fields={'x':FieldSchema(type='integer',required=True),'y':FieldSchema(type='integer',required=True)},
        states={'open':StateDef(transitions={'close':'closed'}),'closed':StateDef(terminal=True)},
        actions={'create':ActionDef(kind='create',open=True),'close':ActionDef(kind='lifecycle',from_states=['open'],to='closed',open=True)},
        extra={'authority':{'fields':{'state':'PROVIDER_AUTO_ACCEPT','x':'PROVIDER_AUTO_ACCEPT','y':'PROVIDER_AUTO_ACCEPT','pending':'REQUIRES_REVIEW','canonical':'CANONICAL_AUTHORITATIVE'}}}))
    adapter=Adapter();reducer=Reducer();extensions=TypeExtensionRegistry();extensions.register_reducer(KIND,reducer)
    runtime=GovernedObjectRuntime(SQLiteStore(path),type_registry=types,adapters=[adapter],extension_registry=extensions,
        providers=ProviderRegistry(),application_registry=ApplicationRegistry())
    if create:
        result=runtime.execute(CreateObject('window',KIND,{'x':1,'y':3},state='open',relationships=[{'source_id':'window','relation':'links','target_id':'target'}]),actor=WRITER)
        assert result.decision=='ALLOW'
    return runtime,types,adapter,reducer

def obs(changes=None,*,state=None,bootstrap=False,webhook=False):
    return ExternalObservation(object_id='window',provider_name='probe',external_id='external',
        source=ObservationSource.WEBHOOK if webhook else ObservationSource.RECONCILIATION,
        changes={k:FieldChange(observed=v) for k,v in (changes or {}).items()},
        state_change=FieldChange(observed=state) if state is not None else None,
        raw_metadata={'bootstrap':bootstrap})

def snapshot(rt):return deepcopy(rt._store.dump_tables())
def canonical(rt):
    raw=snapshot(rt)
    return {k:v for k,v in raw.items() if k not in ('observations','review_cases')}

def invalid_case(path,kind):
    rt,types,a,r=build(path)
    try:
        before=canonical(rt)
        claim=obs({'x':'bad'}) if kind=='type' else obs({'x':4}) if kind=='domain' else obs(state='invented')
        result=rt.ingest_observation(claim)
        assert result.decision=='REJECT' and result.reason=='CANDIDATE_INVALID',result.model_dump()
        assert canonical(rt)==before and a.loads==0
    finally:rt._store.close()

def unavailable_case(path,behavior):
    rt,types,a,r=build(path)
    try:
        a.behavior=behavior;before=snapshot(rt)
        result=rt.ingest_observation(obs({'y':4}))
        assert result.decision=='REJECT' and result.reason=='VALIDATION_UNAVAILABLE',result.model_dump()
        assert result.field_results=={} and result.object_version is None and result.differences==[]
        assert snapshot(rt)==before and a.loads==0
    finally:rt._store.close()

def multi_case(path,kind):
    rt,types,a,r=build(path)
    try:
        if kind=='prefix':
            result=rt.ingest_observation(obs({'x':4,'y':6}))
            assert result.decision=='ACCEPT' and result.field_results=={'x':'REJECT','y':'ACCEPT'}
            assert rt._store.get('window').data=={'x':1,'y':6}
        elif kind=='mixed':
            result=rt.ingest_observation(obs({'canonical':9,'y':4,'pending':7}))
            assert result.decision=='ACCEPT' and result.field_results['canonical']=='REJECT' and result.field_results['pending']=='REQUIRES_REVIEW'
            assert rt._store.get('window').data=={'x':1,'y':4}
        elif kind=='flag':
            result=rt.ingest_observation(obs({'pending':7},bootstrap=True))
            assert result.decision=='REQUIRES_REVIEW' and rt._store.get('window').version==1
        else:
            result=rt.ingest_observation(obs({'x':2,'y':4},state='closed',bootstrap=True))
            assert result.decision=='ACCEPT'
            history=rt.accepted_history('window');assert [e.object_version for e in history]==[1,2,3,4]
            assert [e.history_position for e in history]==[1,2,3,4]
            assert history[-2].details['data']=={'x':2,'y':3}
    finally:rt._store.close()

def review_case(path,kind):
    rt,types,a,r=build(path)
    try:
        rt._observations.observation_store.create_review_case(case_id='case',object_id='window',observation_id='claim',field_name='x',canonical_value=0,observed_value=4 if kind=='invalid' else 1 if kind=='noop' else 2)
        before=canonical(rt)
        if kind=='unavailable':a.behavior='none'
        if kind in ('keep','keep-unavailable'):
            a.behavior='none' if kind=='keep-unavailable' else 'valid'
            result=rt.resolve_review('case',resolution='KEEP_CANONICAL',resolved_by='trusted')
            assert rt._observations.observation_store.get_review_case('case')['state']=='rejected'
            assert canonical(rt)==before
        else:
            result=rt.resolve_review('case',resolution='ACCEPT_EXTERNAL',resolved_by='trusted')
            status=rt._observations.observation_store.get_review_case('case')['state']
            if kind in ('invalid','unavailable'):
                assert result.decision=='REJECT' and status=='open' and canonical(rt)==before
            elif kind=='noop':
                assert result.decision=='NO_CHANGE' and status=='accepted' and canonical(rt)==before
            else:
                assert result.decision=='ACCEPT' and status=='accepted' and rt._store.get('window').data=={'x':2,'y':3}
    finally:rt._store.close()

def registration_case(path,kind):
    rt,types,a,r=build(path,create=False)
    try:
        if kind=='missing':a.initial=None
        if kind=='invalid':a.initial={'x':7,'y':3}
        if kind=='unavailable':a.behavior='base'
        before=snapshot(rt)
        try:
            obj=rt.registration.register_with_binding('registered',KIND,ProviderBinding(provider_name='probe',external_id='e'),context=rt.context)
        except Exception as exc:
            assert kind!='valid' and type(exc).__name__=='RegistrationRefused',type(exc).__name__
            assert exc.code=='REGISTRATION_NOT_ACCEPTED' and exc.args==('registration unavailable',)
            assert snapshot(rt)==before
        else:
            assert kind=='valid','invalid registration accepted'
            history=rt.accepted_history('registered')
            assert len(history)==1 and history[0].details['data']=={'x':1,'y':3}
            assert obj.version==1 and history[0].object_version==1 and history[0].history_position==1
            rt._store._conn.execute("UPDATE objects SET data_json='{}',state='discarded' WHERE id='registered'");rt._store._conn.commit()
            replayed=rt.replay('registered');assert replayed.data=={'x':1,'y':3} and replayed.state=='open' and r.calls==1
    finally:rt._store.close()

def capacity_case(path):
    rt,types,a,r=build(path)
    try:
        rt._store._conn.execute('UPDATE objects SET version_high_water=?',(INT64_MAX-1,));rt._store._conn.commit()
        before=snapshot(rt);result=rt.ingest_observation(obs({'x':2,'y':4}))
        assert result.reason=='OBJECT_VERSION_SPACE_EXHAUSTED' and snapshot(rt)==before
    finally:rt._store.close()

def rollback_case(path,phase):
    rt,types,a,r=build(path)
    try:
        before=snapshot(rt);original=rt._store.append_accepted;count=0
        def failing(event):
            nonlocal count
            count+=1
            result=original(event)
            if count==2:raise RuntimeError('injected-write-failure')
            return result
        if phase=='append':rt._store.append_accepted=failing
        else:
            def fail(*args,**kwargs):raise RuntimeError('injected-write-failure')
            rt._observations.observation_store.save_observation=fail
        try:rt.ingest_observation(obs({'x':2,'y':4}))
        except RuntimeError as exc:assert str(exc)=='injected-write-failure'
        else:raise AssertionError('expected persistence fault')
        assert snapshot(rt)==before
    finally:rt._store.close()

def schema_case(path,kind):
    from governed_objects.observations.validation import CandidateImage,CandidateMode,CandidateVerdict,validate_candidate
    rt,types,a,r=build(path)
    try:
        definition=types.get(KIND).model_copy(deep=True);data={'x':1,'y':3};state='open';current=candidate_image=None
        expected=CandidateVerdict.INVALID
        if kind=='required-none':data['x']=None
        elif kind=='required-missing':del data['x']
        elif kind=='bool-integer':data['x']=True
        elif kind=='numeric-string':data['x']='2'
        elif kind=='nan':data['x']=float('nan')
        elif kind=='infinity':data['y']=float('inf')
        elif kind=='tuple':data['extra']=(1,2)
        elif kind=='bad-key':data['extra']={1:'bad'}
        elif kind=='python-date':
            from datetime import date
            data['extra']=date(2026,1,1)
        elif kind=='cycle':data['extra']=data
        elif kind=='unknown-schema':
            definition.fields['extra']=FieldSchema(type='undeclared-label');expected=CandidateVerdict.UNAVAILABLE
        elif kind in ('array','object','optional-none','native-date-string','extra-json'):
            expected=CandidateVerdict.VALID
            if kind=='array':definition.fields['extra']=FieldSchema(type='array');data['extra']=[{'n':1}]
            if kind=='object':definition.fields['extra']=FieldSchema(type='object');data['extra']={'n':[1]}
            if kind=='optional-none':definition.fields['extra']=FieldSchema(type='string');data['extra']=None
            if kind=='native-date-string':definition.fields['extra']=FieldSchema(type='date');data['extra']='native string baseline'
            if kind=='extra-json':data['extra']={'n':[1,True,None]}
        elif kind=='unknown-state':state='missing'
        elif kind=='terminal':
            current=CandidateImage('window',KIND,VERSION,'closed',{'x':1,'y':3})
        elif kind=='no-states-change':
            definition.states={};state='changed';expected=CandidateVerdict.UNAVAILABLE
        elif kind=='no-states-initial':
            definition.states={};definition.initial_state=None;expected=CandidateVerdict.UNAVAILABLE
        if current is None and kind!='no-states-initial':current=CandidateImage('window',KIND,VERSION,'open',{'x':1,'y':3})
        before=snapshot(rt)
        verdict=validate_candidate(type_def=definition,current=current,candidate=CandidateImage('window',KIND,VERSION,state,data),
            mode=CandidateMode.REGISTRATION_INITIAL if current is None else CandidateMode.OBSERVATION,adapter=a)
        assert verdict is expected,(kind,verdict)
        assert snapshot(rt)==before and a.loads==0
    finally:rt._store.close()

def planning_case(path,kind):
    from governed_objects.domain.command import TransitionState
    rt,types,a,r=build(path)
    try:
        before=snapshot(rt)
        if kind=='unavailable-later':
            def callback():
                if a.calls==2:a.behavior='none'
            a.on_validate=callback
            result=rt.ingest_observation(obs({'x':2,'y':4}))
            assert result.reason=='VALIDATION_UNAVAILABLE' and snapshot(rt)==before and a.calls==2
        elif kind=='config-change':
            a.on_validate=lambda:types.register(types.get(KIND).model_copy(update={'type_version':'replacement'}))
            result=rt.ingest_observation(obs({'y':4}))
            assert result.reason=='VALIDATION_UNAVAILABLE' and snapshot(rt)==before
        elif kind=='independent-change':
            def change():assert rt.execute(TransitionState('window','close'),actor=WRITER).decision=='ALLOW'
            a.on_validate=change
            result=rt.ingest_observation(obs({'y':4}))
            assert result.reason=='CANONICAL_CHANGED'
            assert rt._store.get('window').state=='closed' and rt._store.get('window').data=={'x':1,'y':3}
            assert len(rt.accepted_history('window'))==2
        elif kind=='no-create-fallback':
            def forbidden(*args,**kwargs):raise AssertionError('create is not update validation')
            a.validate_create=forbidden
            result=rt.ingest_observation(obs({'y':4}));assert result.decision=='ACCEPT'
        elif kind=='staged-before-write':
            def check():
                assert snapshot(rt)==before and not rt._store._observation_transaction_active
            a.on_validate=check
            result=rt.ingest_observation(obs({'x':2,'y':4}));assert result.decision=='ACCEPT' and a.calls==2
        elif kind=='invalid-webhook':
            obj=rt._store.get('window');obj.metadata.update(ProviderBinding(provider_name='probe',external_id='e').to_metadata());rt._store.save(obj)
            before=canonical(rt);result=rt.ingest_observation(obs({'x':4},webhook=True))
            assert result.decision=='REJECT' and canonical(rt)==before and a.loads==0
        elif kind=='exact-capacity':
            rt._store._conn.execute('UPDATE objects SET version_high_water=?',(INT64_MAX-2,));rt._store._conn.commit()
            result=rt.ingest_observation(obs({'x':2,'y':4}))
            assert result.decision=='ACCEPT' and result.object_version==INT64_MAX
            assert [e.object_version for e in rt.accepted_history('window')]==[1,INT64_MAX-1,INT64_MAX]
        elif kind=='noop-at-capacity':
            rt._store._conn.execute('UPDATE objects SET version_high_water=?',(INT64_MAX,));rt._store._conn.commit()
            before=canonical(rt);result=rt.ingest_observation(obs({'x':1,'y':3}))
            assert result.decision=='NO_CHANGE' and canonical(rt)==before and a.calls==0
        elif kind in ('invalid-datetime','mixed-unserializable','unpersistable-review'):
            from datetime import datetime
            changes={'x':datetime(2026,1,1)}
            if kind=='mixed-unserializable':changes['y']=4
            if kind=='unpersistable-review':changes={'y':4,'pending':datetime(2026,1,1)}
            result=rt.ingest_observation(obs(changes))
            if kind=='unpersistable-review':
                assert result.reason=='VALIDATION_UNAVAILABLE' and snapshot(rt)==before
            elif kind=='invalid-datetime':
                assert result.decision=='REJECT' and result.reason=='CANDIDATE_INVALID' and canonical(rt)=={k:v for k,v in before.items() if k not in ('observations','review_cases')}
            else:
                assert result.decision=='ACCEPT' and result.field_results=={'x':'REJECT','y':'ACCEPT'}
                assert rt._store.get('window').data=={'x':1,'y':4} and len(rt.accepted_history('window'))==2
    finally:rt._store.close()

def extended_registration(path,kind):
    rt,types,a,r=build(path,create=False)
    try:
        binding=ProviderBinding(provider_name='probe',external_id='e')
        args={}
        if kind in ('local-valid','local-required'):
            binding=ProviderBinding.local_binding()
            if kind=='local-valid':types.get(KIND).fields.clear()
        if kind=='state-mismatch':args['state']='closed'
        if kind=='provider-missing':
            from governed_objects.providers.errors import ProviderObjectNotFound
            def missing(*args):raise ProviderObjectNotFound('synthetic missing')
            a.reader=missing
        if kind in ('write-rollback','edge-rollback'):
            name='append_accepted' if kind=='write-rollback' else 'add';original=getattr(rt._store,name)
            def fail(value):original(value);raise RuntimeError('injected registration failure')
            setattr(rt._store,name,fail)
        before=snapshot(rt)
        try:obj=rt.registration.register_with_binding('registered',KIND,binding,context=rt.context,**args)
        except Exception as exc:
            assert kind!='local-valid'
            if kind=='provider-missing':assert type(exc).__name__=='ProviderObjectNotFound'
            elif kind in ('write-rollback','edge-rollback'):assert type(exc) is RuntimeError
            else:assert type(exc).__name__=='RegistrationRefused'
            assert snapshot(rt)==before
        else:
            assert kind=='local-valid' and obj.data=={} and obj.version==1 and a.loads==0
    finally:rt._store.close()

def reconstruction_case(path):
    rt,types,a,r=build(path)
    try:
        assert rt.ingest_observation(obs({'x':2,'y':4},state='closed',bootstrap=True)).decision=='ACCEPT'
        for position,token in enumerate((40,7,9,31),1):
            rt._store._conn.execute('UPDATE events SET object_version=? WHERE history_position=?',(token,position))
        rt._store._conn.execute("UPDATE objects SET data_json='{}', state='discarded', version=999, version_high_water=999")
        rt._store._conn.commit();rt._store.remove('window','links','target')
        result=rt.replay('window')
        assert result.data=={'x':2,'y':4} and result.state=='closed' and result.version==31 and r.calls==4
        assert [(e.relation,e.target_id) for e in rt.canonical_relationships('window')]==[('links','target')]
        assert rt._store.version_high_water('window')==999
    finally:rt._store.close()

def readonly_case(path,kind):
    from governed_objects.providers.projection import ProviderProjection
    rt,types,a,r=build(path)
    class Raw:
        name='readonly'
        def __init__(self):self.fetches=0;self.writes=0
        def fetch(self,key):self.fetches+=1;return {'data':{'x':1,'y':4},'state':'open','revision':'opaque'}
        def apply_patch(self,*args,**kwargs):self.writes+=1;raise AssertionError('read-only')
    provider=Raw()
    try:
        rt.providers.register(provider)
        descriptor=ProviderProjection(lambda row:(row['data'],row['state']),lambda row:row['revision'],None,'independent-readonly')
        rt.providers.register_projection(provider.name,KIND,VERSION,descriptor)
        def hydrate(obj,context):
            d,_=rt.providers.resolve_projection(provider.name,obj.type,obj.type_version)
            data,state=d.normalize(provider.fetch('e'))
            return HydratedObject(object=obj.model_copy(update={'data':data,'state':state}))
        a.reader=hydrate
        obj=rt._store.get('window');obj.metadata.update(ProviderBinding(provider_name=provider.name,external_id='e').to_metadata());rt._store.save(obj)
        if kind=='completed-sync-unsupported':rt.providers.remove_projection(provider.name,KIND,VERSION)
        if kind=='keep-unsupported':
            rt._observations.observation_store.create_review_case(case_id='case',object_id='window',observation_id='o',field_name='y',canonical_value=3,observed_value=4)
            result=rt.resolve_review('case',resolution='KEEP_CANONICAL',resolved_by='trusted')
            assert result.field_results['y']=='UNSUPPORTED_PROVIDER' and rt._store.get('window').version==1
        else:
            a.on_validate=lambda:(_ for _ in ()).throw(AssertionError('provider used in validation')) if provider.fetches else None
            result=rt.ingest_observation(obs({'y':4}))
            assert result.decision=='ACCEPT' and rt._store.get('window').data['y']==4
            if kind=='completed-sync-unsupported':assert result.field_results['provider_sync']=='UNSUPPORTED_PROVIDER'
            else:assert provider.fetches==1
        assert provider.writes==0
    finally:rt._store.close()

def cases():
    result={}
    for kind in ('type','domain','lifecycle'):result['invalid/'+kind]=lambda p,k=kind:invalid_case(p,k)
    for behavior in ('base','none','raw','throw','mutate','definition-mutate','unavailable'):
        result['unavailable/'+behavior]=lambda p,b=behavior:unavailable_case(p,b)
    for kind in ('prefix','mixed','flag','tokens'):result['multi/'+kind]=lambda p,k=kind:multi_case(p,k)
    for kind in ('invalid','unavailable','noop','valid','keep','keep-unavailable'):
        result['review/'+kind]=lambda p,k=kind:review_case(p,k)
    for kind in ('valid','missing','invalid','unavailable'):result['registration/'+kind]=lambda p,k=kind:registration_case(p,k)
    result['capacity']=capacity_case
    for phase in ('append','observation'):result['rollback/'+phase]=lambda p,k=phase:rollback_case(p,k)
    for kind in ('required-none','required-missing','bool-integer','numeric-string','nan','infinity','tuple','bad-key','python-date','cycle',
                 'unknown-schema','array','object','optional-none','native-date-string','extra-json','unknown-state','terminal','no-states-change','no-states-initial'):
        result['schema/'+kind]=lambda p,k=kind:schema_case(p,k)
    for kind in ('unavailable-later','config-change','independent-change','no-create-fallback','staged-before-write','invalid-webhook','exact-capacity','noop-at-capacity','invalid-datetime','mixed-unserializable','unpersistable-review'):
        result['planning/'+kind]=lambda p,k=kind:planning_case(p,k)
    for kind in ('local-valid','local-required','state-mismatch','provider-missing','write-rollback','edge-rollback'):
        result['registration-extra/'+kind]=lambda p,k=kind:extended_registration(p,k)
    result['genuine-state-edge-reconstruction']=reconstruction_case
    for kind in ('read-only','completed-sync-unsupported','keep-unsupported'):
        result['provider/'+kind]=lambda p,k=kind:readonly_case(p,k)
    return result

def main(path):
    path=Path(path);path.mkdir(exist_ok=False);results={}
    for index,(name,call) in enumerate(cases().items()):
        try:call(path/f'{index}.db');results[name]={'passed':True}
        except Exception as exc:results[name]={'passed':False,'type':type(exc).__name__,'message':str(exc)}
    (path/'results.json').write_text(json.dumps(results,indent=2)+'\n')
    print(json.dumps({'cases':len(results),'passed':sum(v['passed'] for v in results.values())}))
    return 0 if all(v['passed'] for v in results.values()) else 1
if __name__=='__main__':raise SystemExit(main(sys.argv[1]))
