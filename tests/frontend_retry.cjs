/* Contract-level browser-state test; this is not a real browser or model run. */
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const source=fs.readFileSync(path.join(__dirname,'../web/app.js'),'utf8');
const storage=new Map(),requests=[];
let identity=0,failNext=true,rejectNext=false;
const run={id:'a'.repeat(32),task_id:'quantity-shortfall',mode:'demo',state:'queued',version:0,created_at:'2026-10-28T00:00:00Z',checkpoint:{},proposal:null,result:null};
function environment(){
 const elements=new Map();
 const element=()=>({value:'',textContent:'',hidden:false,disabled:false,children:[],append(...x){this.children.push(...x)},replaceChildren(...x){this.children=x},click(){}});
 const document={getElementById(id){if(!elements.has(id))elements.set(id,element());return elements.get(id)},createElement:element,createTextNode:t=>t};
 document.getElementById('mode').value='demo';
 const fetch=async(url,options={})=>{
  let body;
  if(url==='/api/runs'&&options.method==='POST'){
   requests.push({key:options.headers['Idempotency-Key'],body:JSON.parse(options.body)});
   if(failNext){failNext=false;throw new TypeError('response lost after commit')}
   if(rejectNext){rejectNext=false;return{ok:false,status:422,json:async()=>({error:{message:'invalid task'}})}}
   body=run;
  }else if(url==='/api/health')body={container_available:true,model_configured:false,model:'test'};
  else if(url==='/api/tasks')body={tasks:[{id:'quantity-shortfall',title:'Quantity',description:'test'}]};
  else if(url==='/api/runs')body={runs:[]};
  else if(url.endsWith('/events'))body={events:[]};
  else body=run;
  return{ok:true,status:200,json:async()=>body};
 };
 const context=vm.createContext({document,fetch,sessionStorage:{getItem:k=>storage.get(k)||null,setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)},crypto:{randomUUID:()=>`request-${++identity}`},setInterval:()=>0,console,Blob,URL,Date,JSON});
 vm.runInContext(source,context);
 return{context,elements};
}
async function flush(){await new Promise(resolve=>setImmediate(resolve));await new Promise(resolve=>setImmediate(resolve));}
(async()=>{
 let first=environment();await flush();await first.elements.get('start').onclick();
 assert.equal(requests.length,1);assert(storage.has('patchtrail.pendingStart'));
 assert.equal(first.elements.get('start').textContent,'核对上次创建');
 // Reload the page after a lost create response. The next click must reconcile.
 let reloaded=environment();await flush();await reloaded.elements.get('start').onclick();
 assert.equal(requests.length,2);assert.equal(requests[0].key,requests[1].key);assert.deepEqual(requests[0].body,requests[1].body);
 assert.equal(storage.has('patchtrail.pendingStart'),false);
 // A confirmed client-side rejection permits a genuinely new request afterward.
 rejectNext=true;await reloaded.elements.get('start').onclick();const rejected=requests.at(-1).key;
 assert.equal(storage.has('patchtrail.pendingStart'),false);
 await reloaded.elements.get('start').onclick();assert.notEqual(requests.at(-1).key,rejected);
 console.log(JSON.stringify({status:'passed',checks:['lost response preserves request','reload reconciles same request','confirmed rejection allows new request'],scope:'Node VM with explicit DOM/fetch doubles; not browser acceptance'}));
})().catch(error=>{console.error(error);process.exitCode=1});
