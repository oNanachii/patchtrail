const $ = id => document.getElementById(id);
const state = { tasks: [], runs: [], task: null, run: null, busy: false, health: null, selectedId: null };
try { state.pendingStart=JSON.parse(sessionStorage.getItem('patchtrail.pendingStart')||'null'); } catch { state.pendingStart=null; }
function pendingStart(value){state.pendingStart=value;try{if(value)sessionStorage.setItem('patchtrail.pendingStart',JSON.stringify(value));else sessionStorage.removeItem('patchtrail.pendingStart');}catch{}$('start').textContent=value?'核对上次创建':'开始一次排错';}
const labels = {queued:'排队中',diagnosing:'调查中',awaiting_approval:'等待确认',verifying:'检查中',succeeded:'检查通过',failed:'执行失败',cancelled:'已取消',interrupted:'执行中断'};
const eventLabels = {run_created:'创建任务',state_changed:'状态变化',model_requested:'请求模型／脚本动作',tool_requested:'请求工具',tool_result:'工具返回',tool_rejected:'工具被拒绝',verification_started:'开始独立验证',explicit_resume_after_unknown_model_outcome:'明确恢复未确认的模型请求'};
function notice(message='') { $('notice').textContent=message; $('notice').hidden=!message; }
async function api(path, options={}) {
  const token=$('token').value.trim();
  const response=await fetch(path,{...options,headers:{'Content-Type':'application/json',...(token?{Authorization:`Bearer ${token}`} : {}),...options.headers}});
  let body; try { body=await response.json(); } catch { throw new Error('服务未返回有效JSON，请检查运行状态。'); }
  if(!response.ok){const error=new Error(body.error?.message || `请求失败 (${response.status})`);error.confirmedRejection=response.status>=400&&response.status<500;throw error;}
  return body;
}
function taskList() {
  $('tasks').replaceChildren();
  for(const task of state.tasks) {
    const button=document.createElement('button');button.className='task'+(state.task===task.id?' active':'');
    const name=document.createElement('span');name.className='task-label';name.textContent=task.title;
    const description=document.createElement('small');description.textContent=task.description;
    button.append(name,description);button.onclick=()=>{state.task=task.id;taskList();};$('tasks').append(button);
  }
}
function runList() {
  $('runs').replaceChildren();
  if(!state.runs.length){$('runs').textContent='还没有运行记录。';return;}
  for(const run of state.runs){
    const button=document.createElement('button');button.className='run-item'+(state.selectedId===run.id?' selected':'');
    const text=document.createElement('span');text.textContent=(state.tasks.find(t=>t.id===run.task_id)?.title||run.task_id)+' · '+(labels[run.state]||run.state);
    const meta=document.createElement('small');meta.textContent=run.id.slice(0,10)+' · '+(run.mode==='demo'?'脚本演示':'DeepSeek');button.append(text,meta);
    button.onclick=()=>{state.selectedId=run.id;refresh().catch(e=>notice(e.message));};$('runs').append(button);
  }
}
function metric(label,value){const el=document.createElement('div');el.className='metric';const strong=document.createElement('strong');strong.textContent=value;el.append(strong,document.createTextNode(label));return el;}
function showRun(run,events){
  state.run=run;$('empty').hidden=true;$('run-detail').hidden=false;
  $('run-title').textContent=state.tasks.find(t=>t.id===run.task_id)?.title||run.task_id;
  $('state').textContent=labels[run.state]||run.state;$('state').className='badge '+run.state;
  $('run-meta').textContent=`运行 ${run.id} · 版本 ${run.version} · ${new Date(run.created_at).toLocaleString()}`;
  $('provider-label').textContent=run.mode==='demo'?'脚本演示：不调用大模型；验证仍需真实容器执行。':`真实模型调用：${state.health?.model||'DeepSeek'}。结果以检查输出为准。`;
  const cp=run.checkpoint||{};$('metrics').replaceChildren(metric('模型／脚本轮次',cp.calls??0),metric('工具次数',cp.steps??0),metric('执行耗时',`${((cp.elapsed_ms||0)/1000).toFixed(1)}s`),metric('实际Token',run.mode==='demo'?'未调用':cp.usage?.total_tokens??'—'));
  $('cancel').hidden=['succeeded','failed','cancelled'].includes(run.state);$('resume').hidden=run.state!=='interrupted';
  $('proposal-panel').hidden=!run.proposal;
  if(run.proposal){$('diff').textContent=run.proposal.diff;$('explanation').textContent=run.proposal.explanation||'';$('proposal-hash').textContent=`补丁 ${run.proposal.digest} · 基础 ${run.proposal.base_workspace_hash}`;$('approve').hidden=run.state!=='awaiting_approval';}
  $('result-panel').hidden=!run.result;
  if(run.result){$('result-summary').textContent=run.stop_reason||'';$('result').textContent=run.result.output||run.result.error||JSON.stringify(run.result,null,2);}
  $('events').replaceChildren();
  for(const event of events){const box=document.createElement('details');box.className='event';const summary=document.createElement('summary');summary.textContent=eventLabels[event.kind]||event.kind;const stamp=document.createElement('time');stamp.textContent=new Date(event.created_at).toLocaleTimeString();summary.append(stamp);const pre=document.createElement('pre');pre.textContent=JSON.stringify(event.payload,null,2);box.append(summary,pre);$('events').append(box);}
  if(cp.unconfirmed_model_request) notice('上一次模型响应尚未确认。恢复可能产生新的模型请求，请先查看记录。');
}
async function refresh(){
  const selected=state.selectedId;const list=await api('/api/runs');state.runs=list.runs;runList();
  if(!selected)return;
  const [run,events]=await Promise.all([api(`/api/runs/${selected}`),api(`/api/runs/${selected}/events`)]);
  if(state.selectedId===selected)showRun(run,events.events);
}
async function action(fn){if(state.busy)return;state.busy=true;notice();for(const id of ['start','approve','cancel','resume'])$(id).disabled=true;try{await fn();await refresh();}catch(e){notice(e.message);}finally{state.busy=false;for(const id of ['start','approve','cancel','resume'])$(id).disabled=false;}}
$('start').onclick=()=>action(async()=>{if(!state.task&&!state.pendingStart)throw new Error('请先选择任务。');if(!state.pendingStart)pendingStart({key:crypto.randomUUID(),body:{task_id:state.task,mode:$('mode').value}});const pending=state.pendingStart;try{const run=await api('/api/runs',{method:'POST',headers:{'Idempotency-Key':pending.key},body:JSON.stringify(pending.body)});if(typeof run.id!=='string'||!run.id)throw new Error('创建结果尚未确认，请重试核对同一请求。');state.selectedId=run.id;pendingStart(null);}catch(error){if(error.confirmedRejection)pendingStart(null);else error.message+='；再次点击会核对同一请求，避免重复创建。';throw error;}});
$('approve').onclick=()=>action(async()=>{const run=state.run;await api(`/api/runs/${run.id}/approve`,{method:'POST',body:JSON.stringify({proposal_digest:run.proposal.digest,expected_version:run.version})});});
$('cancel').onclick=()=>action(async()=>{await api(`/api/runs/${state.run.id}/cancel`,{method:'POST'});});
$('resume').onclick=()=>action(async()=>{await api(`/api/runs/${state.run.id}/resume`,{method:'POST',body:JSON.stringify({expected_version:state.run.version})});});
$('download').onclick=()=>action(async()=>{const report=await api(`/api/runs/${state.run.id}/report`);const url=URL.createObjectURL(new Blob([JSON.stringify(report,null,2)],{type:'application/json'}));const anchor=document.createElement('a');anchor.href=url;anchor.download=`patchtrail-${state.run.id}.json`;anchor.click();URL.revokeObjectURL(url);});
async function boot(){try{const [health,tasks]=await Promise.all([api('/api/health'),api('/api/tasks')]);state.health=health;state.tasks=tasks.tasks;state.task=state.pendingStart?.body?.task_id||state.tasks[0]?.id;if(state.pendingStart){$('mode').value=state.pendingStart.body.mode;pendingStart(state.pendingStart);notice('检测到尚未确认的创建请求，请点击核对上次创建。');}taskList();$('health').textContent=`API正常 · 容器${health.container_available?'可用':'未就绪'} · DeepSeek${health.model_configured?'已配置':'未配置'}`;await refresh();}catch(e){notice(e.message);$('health').textContent='服务连接失败';}}
boot();setInterval(()=>{if(!state.busy)refresh().catch(e=>notice(e.message));},1800);
