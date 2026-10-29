import test from 'node:test';
import assert from 'node:assert/strict';
import { bindImportRequest, createCatalogGeneration, createImportGeneration, commitOutcome, editablePath, inspectFiles, readUploadBody, validImportedTask, validImportPreview } from '../src/lib/importState.js';
import { createAuthEpoch, verifyOperatorAccess } from '../src/lib/authEpoch.js';

const encoder=new TextEncoder();
function file(name,text,relative='') {
  const bytes=typeof text==='string'?encoder.encode(text):text;
  return { name,size:bytes.byteLength,webkitRelativePath:relative,reads:0,
    async arrayBuffer(){this.reads++;return bytes.buffer.slice(bytes.byteOffset,bytes.byteOffset+bytes.byteLength);} };
}
function state(input) {
  const found=inspectFiles(input);
  return {files:found.files,title:'owned example',description:'Fix the explicitly selected behavior',profile:'pytest',testPath:found.testPath};
}
function deferred(){let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return {promise,resolve,reject};}

test('folder selection strips only the root, excludes secrets/generated paths before reading, and keeps tests readonly',()=>{
  const input=[
    file('app.py','VALUE=1\n','中文目录 with spaces/src/app.py'),
    file('test_app.py','def test_one(): pass\n','中文目录 with spaces/tests/test_app.py'),
    file('.env','private value','中文目录 with spaces/.env'),
    file('tool.py','generated','中文目录 with spaces/.venv/tool.py'),
    file('settings_local.py','private config','中文目录 with spaces/settings_local.py'),
  ];
  const found=inspectFiles(input,true);
  assert.deepEqual(found.files.map(row=>row.path),['settings_local.py','src/app.py','tests/test_app.py']);
  assert.equal(found.excluded.length,2);
  assert.equal(found.testPath,'tests');
  assert.deepEqual(found.files.filter(row=>row.editable).map(row=>row.path),['src/app.py']);
  assert.ok(input.every(row=>row.reads===0));
  assert.equal(editablePath('configs/worker.py'),false);
  assert.equal(editablePath('test_app.py'),false);
});

test('flat file upload reads only selected files and creates a fixed manifest without a shell or image',async()=>{
  const source=file('calculator.py','def answer(): return 0\n');
  const tests=file('test_calculator.py','def test_answer(): pass\n');
  const readme=file('README.md','not selected by default');
  const body=await readUploadBody(state([source,tests,readme]));
  assert.deepEqual(body.files.map(row=>row.path),['calculator.py','test_calculator.py']);
  assert.deepEqual(body.manifest.editable_files,['calculator.py']);
  assert.deepEqual(body.manifest.test_paths,['test_calculator.py']);
  assert.equal(body.manifest.check_profile,'pytest');
  assert.equal(readme.reads,0);
  assert.equal('command' in body.manifest,false);
  assert.equal('image' in body.manifest,false);
});

test('strict file decoding preserves UTF8 BOM but rejects malformed bytes and NUL before any request',async()=>{
  const tests=file('test_app.py','def test_x(): pass\n');
  const bom=encoder.encode('\ufeffVALUE=1\n');
  const body=await readUploadBody(state([file('app.py',bom),tests]));
  assert.deepEqual(encoder.encode(body.files[0].content),bom);
  await assert.rejects(readUploadBody(state([file('app.py',new Uint8Array([255,254])),tests])),/不是有效UTF-8/);
  await assert.rejects(readUploadBody(state([file('app.py','x\0y'),tests])),/NUL/);
});

test('a delayed file read cannot repopulate a replaced selection or cleared credential form',async()=>{
  const wait=deferred(),guard=createImportGeneration(),source=file('app.py','VALUE=1\n');
  source.arrayBuffer=()=>wait.promise;
  const ticket=guard.capture();
  const pending=guard.guard(ticket,()=>readUploadBody(state([source,file('test_app.py','def test_x(): pass\n')])));
  guard.advance();
  wait.resolve(encoder.encode('VALUE=1\n').buffer);
  await assert.rejects(pending,error=>error.name==='AbortError');
});

test('old preview success and old error are both discarded after an edited problem or file selection',async()=>{
  for(const failed of [false,true]){
    const wait=deferred(),guard=createImportGeneration(),ticket=guard.capture();
    const pending=guard.guard(ticket,()=>wait.promise);guard.advance();
    if(failed)wait.reject(new Error('old upload failed'));else wait.resolve({preview_id:'old-preview'});
    await assert.rejects(pending,error=>error.name==='AbortError');
  }
});

test('malformed success, redirect and transport errors keep the same confirmation identity actionable',()=>{
  for(const error of [{status:200,code:'invalid_response'},{status:201},{status:302},{status:0,network:true},{status:503}]){
    assert.equal(commitOutcome(error),'unknown');
  }
  for(const status of [400,404,409,410,429])assert.equal(commitOutcome({status}),'rejected');
});

test('a catalog request started before commit cannot remove the newly imported and selected task',async()=>{
  const wait=deferred(),catalog=createCatalogGeneration(),ticket=catalog.capture();
  let tasks=[{id:'builtin'}];
  const response=wait.promise.then(incoming=>{if(catalog.isCurrent(ticket))tasks=incoming;});
  catalog.advance();tasks=[{id:'new-private-task'},...tasks];wait.resolve([{id:'builtin'}]);await response;
  assert.equal(tasks[0].id,'new-private-task');
  const fresh=catalog.capture();assert.equal(catalog.isCurrent(fresh),true);
});

test('a credential change blocks a late file read before network dispatch even before React cleanup',async()=>{
  const wait=deferred(),source=file('app.py','VALUE=1\n'),localGeneration=createImportGeneration();
  let credentialEpoch=0,calls=0;
  source.arrayBuffer=()=>wait.promise;
  const request=bindImportRequest(async()=>{calls++;return {preview_id:'should-not-be-sent'};},credentialEpoch,value=>value===credentialEpoch);
  const pending=(async()=>{
    const body=await localGeneration.guard(localGeneration.capture(),()=>readUploadBody(state([source,file('test_app.py','def test_x(): pass\n')])));
    return request('/api/imports/preview',{method:'POST',body});
  })();
  credentialEpoch++; // Deliberately no local cleanup/invalidation.
  wait.resolve(encoder.encode('VALUE=1\n').buffer);
  await assert.rejects(pending,error=>error.name==='AbortError');
  assert.equal(calls,0);
});

test('buffered import responses and errors cannot cross a credential boundary',async()=>{
  for(const failed of [false,true]){
    const wait=deferred();let credentialEpoch=0;
    const request=bindImportRequest(()=>wait.promise,credentialEpoch,value=>value===credentialEpoch);
    const pending=request('/api/imports/commit',{method:'POST',body:{preview_id:'same-id'}});
    credentialEpoch++;
    if(failed)wait.reject(new Error('old401'));else wait.resolve({task:{id:'private-task'}});
    await assert.rejects(pending,error=>error.name==='AbortError');
  }
});

test('malformed200 responses cannot enter the preview or task rendering states',()=>{
  const preview={preview_id:'upload-'+'a'.repeat(64),manifest:{title:'Owned'},file_count:2,total_bytes:4,files:[
    {path:'app.py',bytes:2,sha256:'b'.repeat(64)},{path:'test_app.py',bytes:2,sha256:'c'.repeat(64)}
  ]};
  assert.equal(validImportPreview(preview),true);
  for(const value of [{},{...preview,file_count:9},{...preview,total_bytes:8},{...preview,files:[preview.files[0],preview.files[0]]}]){
    assert.equal(validImportPreview(value),false);
  }
  const task={private:true,id:'project-'+'d'.repeat(56),title:'Owned',description:'Fixed behavior',editable_files:['app.py'],tags:['web-import'],source_hash:'e'.repeat(64)};
  assert.equal(validImportedTask(task),true);
  assert.equal(validImportedTask({...task,tags:null}),false);
  assert.equal(validImportedTask({...task,private:false}),false);
});

test('read-only operator verification cannot accept an old credential after a new one is validated',async()=>{
  const epochs=createAuthEpoch(),oldReply=deferred(),newReply=deferred();
  let accepted=null;
  async function verify(reply,ticket){
    await verifyOperatorAccess(async(path)=>{assert.equal(path,'/api/projects');return reply.promise;},epochs,ticket);
    epochs.assertCurrent(ticket);
    accepted=ticket;
  }
  const oldTicket=epochs.capture(),oldCheck=verify(oldReply,oldTicket);
  const newTicket=epochs.advance(),newCheck=verify(newReply,newTicket);
  newReply.resolve({projects:[]});await newCheck;assert.equal(accepted,newTicket);
  oldReply.resolve({projects:[{name:'old-private-project'}]});
  await assert.rejects(oldCheck,error=>error.name==='AbortError');
  assert.equal(accepted,newTicket);
});

test('late401 validation is obsolete and malformed success never confirms permission',async()=>{
  const epochs=createAuthEpoch(),oldReply=deferred(),ticket=epochs.capture();
  const old=verifyOperatorAccess(()=>oldReply.promise,epochs,ticket);
  epochs.advance();oldReply.reject(Object.assign(new Error('old denied'),{status:401}));
  await assert.rejects(old,error=>error.name==='AbortError');
  await assert.rejects(verifyOperatorAccess(async()=>({ok:true}),epochs,epochs.capture()),error=>error.code==='invalid_response');
  const current401=Object.assign(new Error('current denied'),{status:401});
  await assert.rejects(verifyOperatorAccess(async()=>{throw current401;},epochs,epochs.capture()),error=>error===current401);
});
