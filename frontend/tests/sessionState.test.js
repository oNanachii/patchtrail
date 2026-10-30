/* BYOK/browser state contracts with explicit response doubles; no model or browser network. */
import assert from 'node:assert/strict';
import test from 'node:test';
import { ApiError, createApi } from '../src/lib/api.js';
import { createAuthEpoch } from '../src/lib/authEpoch.js';
import { PENDING_KEY, createPendingRegistry, pendingStore, submitCreate, validPending } from '../src/lib/createGuard.js';
import { canUsePreflight, connectInvitation, isSecureCredentialOrigin, sessionScope, validPreflight, validSession } from '../src/lib/sessionState.js';
import { findBaseline } from '../src/lib/trail.js';

const MEMBER = { role:'member',member_id:'a'.repeat(32),expires_at:2000000000,model:'deepseek-flash',session_token:'page-memory-session-one' };
const TASK = { id:'quantity-shortfall',source_hash:'b'.repeat(64) };
const PREFLIGHT = { preflight_id:'c'.repeat(32),task_id:TASK.id,source_hash:TASK.source_hash,check_profile:'unittest',
  status:'ready',eligible:true,expires_at:2000000000,check:{exit_code:1,output:'Ran 4 tests\nFAILED',tests_run:4,passed:false} };
const SELECTION = { task_id:TASK.id,mode:'live',preflight_id:PREFLIGHT.preflight_id };
const RUN = {id:'d'.repeat(32),task_id:TASK.id,mode:'live',state:'queued',version:0};

function deferred() {
  let resolve,reject;
  const promise=new Promise((ok,fail)=>{resolve=ok;reject=fail;});
  return {promise,resolve,reject};
}
function memoryStorage() {
  const map=new Map();
  return {map,getItem:key=>map.get(key)??null,setItem:(key,value)=>map.set(key,String(value)),removeItem:key=>map.delete(key)};
}
let keyNumber=0;
const newKey=()=>`byok-request-${++keyNumber}`;

function login(request,epochs,discard=async()=>{}) {
  return connectInvitation({request,invitation:'private-invitation',apiKey:'private-deepseek-key',epochs,ticket:epochs.capture(),discard});
}

test('credential connections require HTTPS except explicit local loopback development origins',()=>{
  for(const origin of ['https://patchtrail.example','https://localhost:3110','http://localhost:3110','http://127.0.0.1:3110','http://[::1]:3110']){
    assert.equal(isSecureCredentialOrigin(origin),true,origin);
  }
  for(const origin of ['http://patchtrail.example','http://192.168.1.10:3110','http://localhost.attacker.example',
    'http://127.0.0.1.attacker.example','ftp://localhost','javascript:alert(1)','not a URL']){
    assert.equal(isSecureCredentialOrigin(origin),false,origin);
  }
});

test('insecure remote login rejects before transmitting either invitation or API key',async()=>{
  const epochs=createAuthEpoch();let requests=0,discards=0;
  await assert.rejects(connectInvitation({origin:'http://patchtrail.example',invitation:'private-invitation',apiKey:'private-deepseek-key',
    epochs,ticket:epochs.capture(),request:async()=>{requests++;return MEMBER;},discard:async()=>{discards++;}}));
  assert.equal(requests,0);
  assert.equal(discards,0);
});

test('the API client itself refuses bearer credentials on remote HTTP before fetch',async()=>{
  const previous=Object.getOwnPropertyDescriptor(globalThis,'location');
  let calls=0;
  const response={ok:true,status:200,json:async()=>({status:'ok'})};
  const fetchImpl=async()=>{calls++;return response;};
  try {
    Object.defineProperty(globalThis,'location',{configurable:true,value:{origin:'http://patchtrail.example'}});
    const authenticated=createApi({fetchImpl,getToken:()=>MEMBER.session_token});
    await assert.rejects(authenticated('/api/session'),error=>error.code==='insecure_origin');
    assert.equal(calls,0);
    const publicRead=createApi({fetchImpl,getToken:()=>''});
    assert.deepEqual(await publicRead('/api/health'),{status:'ok'});
    assert.equal(calls,1);
    Object.defineProperty(globalThis,'location',{configurable:true,value:{origin:'http://127.0.0.1:3110'}});
    assert.deepEqual(await authenticated('/api/session'),{status:'ok'});
    assert.equal(calls,2);
  } finally {
    if(previous)Object.defineProperty(globalThis,'location',previous);else delete globalThis.location;
  }
});

test('invitation connection sends only POST session, with no run, upload or model-triggering request',async()=>{
  const epochs=createAuthEpoch(),sent=[];
  const result=await login(async(path,options)=>{sent.push({path,options});return MEMBER;},epochs);
  assert.equal(result,MEMBER);
  assert.deepEqual(sent,[{path:'/api/session',options:{method:'POST',body:{invite_code:'private-invitation',api_key:'private-deepseek-key'}}}]);
  assert.equal(validSession(result),true);
  assert.equal(sessionScope(result),'member-'+MEMBER.member_id);
});

test('closing or changing the login discards its late session and never accepts it',async()=>{
  const epochs=createAuthEpoch(),reply=deferred(),revocations=[];
  let accepted=null;
  const pending=login(()=>reply.promise,epochs,async token=>{revocations.push(token);}).then(value=>{accepted=value;});
  epochs.advance();
  reply.resolve(MEMBER);
  await assert.rejects(pending,error=>error.name==='AbortError');
  assert.equal(accepted,null);
  assert.deepEqual(revocations,[MEMBER.session_token]);
});

test('an obsolete login does not wait for its session-discard response',async()=>{
  const epochs=createAuthEpoch(),reply=deferred(),discard=deferred();
  const pending=login(()=>reply.promise,epochs,()=>discard.promise);
  epochs.advance();reply.resolve(MEMBER);
  await assert.rejects(pending,error=>error.name==='AbortError');
  // A failed logout is not permission to apply the old login in this page.
  discard.reject(new Error('discard transport unavailable'));
  await Promise.resolve();
});

test('a late old login error cannot overwrite a newly connected identity',async()=>{
  const epochs=createAuthEpoch(),old=deferred();
  let accepted=null,visibleError=null;
  const pending=login(()=>old.promise,epochs).then(value=>{accepted=value;},error=>{
    if(error.name!=='AbortError')visibleError=error.message;
    throw error;
  });
  epochs.advance();
  const current={...MEMBER,member_id:'e'.repeat(32),session_token:'new-page-session'};
  accepted=await login(async()=>current,epochs);
  old.reject(new ApiError({status:401,code:'invalid_invitation',message:'old invitation denied'}));
  await assert.rejects(pending,error=>error.name==='AbortError');
  assert.equal(accepted,current);
  assert.equal(visibleError,null);
});

test('an obsolete malformed login response is ignored rather than displaying an old validation error',async()=>{
  const epochs=createAuthEpoch(),reply=deferred();
  const pending=login(()=>reply.promise,epochs);
  epochs.advance();reply.resolve({role:'member',session_token:'unconfirmed-old-response'});
  await assert.rejects(pending,error=>error.name==='AbortError');
});

test('current login errors remain actionable and malformed login success never creates a session',async()=>{
  const epochs=createAuthEpoch();
  const failure=new ApiError({status:401,code:'invalid_invitation',message:'current invitation denied'});
  await assert.rejects(login(async()=>{throw failure;},epochs),error=>error===failure);
  for(const result of [{},{...MEMBER,session_token:''},{...MEMBER,role:'operator'},{...MEMBER,member_id:'not-a-member-id'}]){
    await assert.rejects(login(async()=>result,epochs),error=>error.code==='invalid_response');
  }
});

test('pending creates follow stable member identity, survive same-member login, and stay hidden from other scopes',async()=>{
  const storage=memoryStorage();
  const firstScope=sessionScope(MEMBER);
  const first=pendingStore(storage,firstScope);
  const calls=[];
  await assert.rejects(submitCreate({store:first,selection:SELECTION,newKey,request:async(path,options)=>{
    calls.push({path,options});throw new ApiError({status:0,code:'network_error',message:'lost response',network:true});
  }}));
  const original=first.get();
  const relogged={...MEMBER,session_token:'different-token-same-member',expires_at:MEMBER.expires_at+1000};
  assert.equal(sessionScope(relogged),firstScope);
  const restored=pendingStore(storage,sessionScope(relogged));
  assert.deepEqual(restored.get(),original);
  for(const identity of [{...MEMBER,member_id:'f'.repeat(32)},{role:'operator',model:MEMBER.model},null]){
    assert.equal(pendingStore(storage,sessionScope(identity)).get(),null);
  }
  const response=await submitCreate({store:restored,selection:{task_id:'batch-expiry',mode:'live',preflight_id:'9'.repeat(32)},newKey,
    request:async(path,options)=>{calls.push({path,options});return RUN;}});
  assert.equal(response.reused,true);
  assert.equal(calls[1].options.headers['Idempotency-Key'],original.key);
  assert.deepEqual(calls[1].options.body,SELECTION);
  assert.equal(restored.get(),null);
  assert.equal(storage.map.size,0);
});

test('blocked browser storage still preserves each identity pending across same-page logout and relogin',async()=>{
  const blocked={getItem(){throw new Error('storage unavailable');},setItem(){throw new Error('storage unavailable');},removeItem(){throw new Error('storage unavailable');}};
  const registry=createPendingRegistry(blocked);
  const scopeA=sessionScope(MEMBER),scopeB=sessionScope({...MEMBER,member_id:'f'.repeat(32)});
  const alice=registry.forScope(scopeA);
  const lost=async()=>{throw new ApiError({status:0,code:'network_error',message:'unknown result',network:true});};
  await assert.rejects(submitCreate({store:alice,selection:SELECTION,newKey,request:lost}));
  const original=alice.get();
  assert.equal(registry.forScope('public').get(),null);
  const bob=registry.forScope(scopeB);
  assert.equal(bob.get(),null);
  await assert.rejects(submitCreate({store:bob,selection:{...SELECTION,preflight_id:'9'.repeat(32)},newKey,request:lost}));
  const bobPending=bob.get();
  const back=registry.forScope(sessionScope({...MEMBER,session_token:'new-memory-only-session'}));
  assert.equal(back,alice);
  assert.deepEqual(back.get(),original);
  const sent=[];
  await submitCreate({store:back,selection:{task_id:'new-selection',mode:'demo'},newKey,request:async(path,options)=>{sent.push({path,options});return RUN;}});
  assert.equal(sent[0].options.headers['Idempotency-Key'],original.key);
  assert.deepEqual(sent[0].options.body,original.body);
  assert.deepEqual(registry.forScope(scopeB).get(),bobPending,'reconciling Alice never clears Bob');
});

test('upgrade adopts legacy pending only into its verified operator or public-demo namespace',()=>{
  for(const mode of ['live','demo']){
    const storage=memoryStorage();
    const original={key:'legacy-upgrade-request',body:{task_id:TASK.id,mode}};
    pendingStore(storage).set(original); // v0.3 default unscoped storage format.
    assert.ok(storage.map.has(PENDING_KEY));
    assert.equal(pendingStore(storage,sessionScope(MEMBER)).get(),null);
    const wrongScope=mode==='live'?'public':'operator';
    assert.equal(pendingStore(storage,wrongScope).get(),null);
    assert.ok(storage.map.has(PENDING_KEY),'wrong identity must leave legacy receipt untouched');
    const ownerScope=mode==='live'?'operator':'public';
    const adopted=pendingStore(storage,ownerScope);
    assert.deepEqual(adopted.get(),original);
    assert.equal(storage.map.has(PENDING_KEY),false,'legacy receipt moves only after the matching identity loads it');
    assert.deepEqual(pendingStore(storage,ownerScope).get(),original,'the adopted request survives another reload');
  }
});

test('an existing scoped pending is reconciled first and then the untouched legacy request is recovered',async()=>{
  const storage=memoryStorage();
  const current={key:'current-scoped-request',body:SELECTION};
  const legacy={key:'legacy-second-request',body:{task_id:'batch-expiry',mode:'live'}};
  pendingStore(storage,'operator').set(current);
  pendingStore(storage).set(legacy);
  const scoped=pendingStore(storage,'operator');
  assert.deepEqual(scoped.get(),current,'migration must not overwrite an unknown request');
  assert.ok(storage.map.has(PENDING_KEY));
  const calls=[];
  const request=async(path,options)=>{calls.push({path,options});return {...RUN,task_id:options.body.task_id};};
  await submitCreate({store:scoped,request,selection:{task_id:'ignored-selection',mode:'demo'},newKey});
  assert.deepEqual(scoped.get(),legacy,'clearing the reconciled request exposes the second receipt without discarding it');
  await submitCreate({store:scoped,request,selection:SELECTION,newKey});
  assert.deepEqual(calls.map(call=>call.options.headers['Idempotency-Key']),[current.key,legacy.key]);
  assert.deepEqual(calls.map(call=>call.options.body),[current.body,legacy.body]);
  assert.equal(scoped.get(),null);
  assert.equal(storage.map.size,0);
});

test('preflight identity survives exact-body unknown retries and no session credential is stored',async()=>{
  const storage=memoryStorage(),store=pendingStore(storage,sessionScope(MEMBER)),sent=[];
  const request=createApi({getToken:()=>MEMBER.session_token,fetchImpl:async(path,options)=>{
    sent.push({path,options});
    if(sent.length===1)throw new TypeError('response lost');
    return {ok:true,status:201,json:async()=>RUN};
  }});
  await assert.rejects(submitCreate({store,request,selection:SELECTION,newKey}),error=>error.createOutcome==='unknown');
  const serialized=[...storage.map.values()].join('');
  assert.ok(serialized.includes(PREFLIGHT.preflight_id));
  for(const secret of [MEMBER.session_token,'private-invitation','private-deepseek-key'])assert.ok(!serialized.includes(secret));
  const restored=pendingStore(storage,sessionScope(MEMBER));
  await submitCreate({store:restored,request,selection:{...SELECTION,preflight_id:'0'.repeat(32)},newKey});
  assert.equal(sent[0].options.headers['Idempotency-Key'],sent[1].options.headers['Idempotency-Key']);
  assert.equal(sent[0].options.body,sent[1].options.body);
  assert.deepEqual(JSON.parse(sent[1].options.body),SELECTION);
  assert.equal(validPending({key:'valid-request',body:{...SELECTION,preflight_id:'bad'}}),null);
});

test('fresh preflight or member quota rejection clears pending, but rejection cannot erase an unknown earlier create',async()=>{
  for(const [status,code] of [[409,'preflight_required'],[429,'member_quota']]){
    const failure=()=>new ApiError({status,code,message:'explicit rejection'});
    const fresh=pendingStore(null);
    await assert.rejects(submitCreate({store:fresh,selection:SELECTION,newKey,request:async()=>{throw failure();}}),error=>error.createOutcome==='rejected');
    assert.equal(fresh.get(),null);
    const uncertain=pendingStore(null);
    await assert.rejects(submitCreate({store:uncertain,selection:SELECTION,newKey,request:async()=>{throw new ApiError({status:0,code:'network_error',message:'unknown',network:true});}}));
    const original=uncertain.get();
    await assert.rejects(submitCreate({store:uncertain,selection:SELECTION,newKey,request:async()=>{throw failure();}}),error=>error.createOutcome==='unknown');
    assert.deepEqual(uncertain.get(),original);
  }
});

test('preflight validation binds task, source, status and expiry before allowing a paid create',()=>{
  assert.equal(validPreflight(PREFLIGHT,TASK),true);
  assert.equal(canUsePreflight(PREFLIGHT,TASK,(PREFLIGHT.expires_at-1)*1000),true);
  assert.equal(canUsePreflight(PREFLIGHT,TASK,PREFLIGHT.expires_at*1000),false);
  assert.equal(canUsePreflight(PREFLIGHT,null,0),false);
  for(const changed of [{...TASK,id:'batch-expiry'},{...TASK,source_hash:'0'.repeat(64)}]){
    assert.equal(validPreflight(PREFLIGHT,changed),false);
    assert.equal(canUsePreflight(PREFLIGHT,changed,0),false);
  }
  for(const status of ['already_passed','environment_missing','timed_out','unconfirmed']){
    const result={...PREFLIGHT,status,eligible:false};
    assert.equal(validPreflight(result,TASK),true);
    assert.equal(canUsePreflight(result,TASK,0),false);
  }
  for(const result of [{},{...PREFLIGHT,preflight_id:'not-an-id'},{...PREFLIGHT,status:'invented'},
    {...PREFLIGHT,eligible:false},{...PREFLIGHT,status:'already_passed'}, {...PREFLIGHT,expires_at:'2000000000'}]){
    assert.equal(validPreflight(result,TASK),false);
  }
});

test('blob export completes from its HTTP body without awaiting a browser download event',async()=>{
  const archive=new Blob([new Uint8Array([0x50,0x4b,0x03,0x04])],{type:'application/zip'});
  let jsonReads=0;
  const request=createApi({getToken:()=>MEMBER.session_token,fetchImpl:async(path,options)=>{
    assert.equal(path,'/api/runs/'+RUN.id+'/files.zip');
    assert.equal(options.headers.Authorization,'Bearer '+MEMBER.session_token);
    return {ok:true,status:200,blob:async()=>archive,json:async()=>{jsonReads++;throw new Error('ZIP is not JSON');}};
  }});
  assert.equal(await request('/api/runs/'+RUN.id+'/files.zip',{responseType:'blob'}),archive);
  assert.equal(jsonReads,0);
});

test('a denied blob export still parses the API error and never treats it as a ZIP',async()=>{
  let blobReads=0;
  const request=createApi({getToken:()=>MEMBER.session_token,fetchImpl:async()=>({ok:false,status:401,
    blob:async()=>{blobReads++;return new Blob();},json:async()=>({error:{code:'session_expired',message:'Sign in again'}})})});
  await assert.rejects(request('/api/runs/'+RUN.id+'/files.zip',{responseType:'blob'}),error=>error instanceof ApiError
    &&error.status===401&&error.code==='session_expired');
  assert.equal(blobReads,0);
});

test('baseline display reads the pre-model baseline_checked event and keeps old run_check compatibility',()=>{
  const baseline=PREFLIGHT.check;
  const application={kind:'baseline_checked',payload:{...PREFLIGHT,check:baseline}};
  const legacy={kind:'tool_result',payload:{name:'run_check',result:{...baseline,output:'legacy baseline output'}}};
  assert.equal(findBaseline([application]),baseline);
  assert.equal(findBaseline([application,legacy]),baseline);
  assert.equal(findBaseline([legacy]),legacy.payload.result);
  assert.equal(findBaseline([{kind:'baseline_checked',payload:{check:{error:'container unavailable'}}}]),null);
});
