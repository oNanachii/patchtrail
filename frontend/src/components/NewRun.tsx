import { useEffect, useRef, useState } from "react";
import type { PendingCreate } from "../lib/createGuard.js";
import { shortHash } from "../lib/format.js";
import type { Health, Task, TokenStatus, SessionInfo, Preflight } from "../types";
import { Dialog, ErrorLine, Icon, ModeTag, Spinner } from "./ui";
import { ImportProjectDialog } from "./ImportProject";
import type { Request } from "../lib/api.js";
import { bindImportRequest } from "../lib/importState.js";
import { createAuthEpoch } from "../lib/authEpoch.js";
import { canUsePreflight, validPreflight } from "../lib/sessionState.js";
import { describeError } from "../lib/labels.js";

type Mode = "demo" | "live";
export type CreateFailure = { message: string; code: string; status: number; server?: string; outcome: string };
const PREFLIGHT_LABELS: Record<Preflight["status"], string> = {
  ready: "已复现原始测试失败，可以继续", already_passed: "原始测试已通过，无需启动修复",
  environment_missing: "缺少测试环境或依赖", timed_out: "预检超时，尚不能确认失败原因", unconfirmed: "未确认有效测试结果",
};

export function NewRun({ tasks, tasksError, health, online, token, tokenStatus, session, pending, busy, failure, onStart, onOperator, request, credentialEpoch, onImported, isCredentialCurrent, operatorCheck, onVerifyOperator }: {
  tasks: Task[] | null; tasksError: string | null; health: Health | null; online: boolean;
  token: string; tokenStatus: TokenStatus; session: SessionInfo | null; pending: PendingCreate | null;
  busy: boolean; failure: CreateFailure | null;
  onStart: (selection: { task_id: string; mode: Mode; preflight_id?: string }) => void;
  onOperator: () => void; request: Request; credentialEpoch: number;
  isCredentialCurrent: (epoch:number) => boolean;
  operatorCheck: {checking:boolean;error:string|null}; onVerifyOperator: () => void;
  onImported: (task: Task, credentialEpoch: number) => boolean;
}) {
  const [taskId, setTaskId] = useState<string | null>(null);
  const [mode, setMode] = useState<Mode>("demo");
  const [confirmation, setConfirmation] = useState<{task:Task;flight:Preflight;epoch:number}|null>(null);
  const [importOpen, setImportOpen] = useState(false);
  const [importedNotice, setImportedNotice] = useState<{message:string;epoch:number}|null>(null);
  const [preflight, setPreflight] = useState<{key:string;result:Preflight}|null>(null);
  const [checking, setChecking] = useState(false);
  const [checkError, setCheckError] = useState("");
  const checkEpoch = useRef(createAuthEpoch());
  const selectionRef = useRef("");
  const locked = Boolean(pending);
  const chosenTask = locked ? pending!.body.task_id : taskId;
  const task = tasks?.find((t) => t.id === chosenTask) ?? null;
  const chosenMode: Mode = locked ? pending!.body.mode : task?.private ? "live" : mode;
  const selectionKey = `${chosenTask}:${task?.source_hash}:${credentialEpoch}`;
  selectionRef.current = selectionKey;
  const flight = preflight?.key === selectionKey ? preflight.result : null;
  const ready = canUsePreflight(flight,task);
  const importRequest = bindImportRequest(request,credentialEpoch,isCredentialCurrent);
  const invalidate = () => { checkEpoch.current.advance(); setPreflight(null); setConfirmation(null); setChecking(false); setCheckError(""); };
  useEffect(() => { invalidate(); }, [selectionKey]);
  useEffect(() => { setImportedNotice(null); }, [credentialEpoch]);
  useEffect(() => () => { checkEpoch.current.advance(); }, []);
  useEffect(() => {
    if (tasks?.length && !tasks.some((item) => item.id === taskId)) setTaskId(tasks[0].id);
  }, [tasks, taskId]);
  const blockers: { text: string; action?: "operator" | "verify" }[] = [];
  if (!online) blockers.push({text:"服务暂时失联，请恢复连接后再试。"});
  if (chosenMode === "live") {
    if (!token) blockers.push({text:"先使用邀请码和自己的DeepSeek Key连接。",action:"operator"});
    else if (tokenStatus !== "accepted" || !session) blockers.push({text:operatorCheck.checking ? "正在确认访问权限，不调用模型…" : "访问权限尚未确认，请重新验证。",action:"verify"});
    else if (session.role === "operator" && !health?.model_configured) blockers.push({text:"原操作者的服务端模型Key尚未配置；成员可使用自己的Key连接。",action:"operator"});
  } else if (!locked && health && !health.container_available) blockers.push({text:"容器测试环境不可用，暂时不能运行演示。"});
  if (!health && online) blockers.push({text:"正在读取服务状态…"});
  const canStart = !busy && !checking && Boolean(chosenTask) && blockers.length === 0 && (locked || chosenMode === "demo" || ready);
  const canCheck = !locked && !busy && !checking && Boolean(task && token && tokenStatus === "accepted" && online);
  const confirmationValid = Boolean(confirmation && isCredentialCurrent(confirmation.epoch) && confirmation.task.id === task?.id
    && confirmation.task.source_hash === task?.source_hash && canUsePreflight(confirmation.flight,task));

  async function check() {
    if (!task || !canCheck) return;
    const ticket = checkEpoch.current.advance(), key = selectionKey, selected = task, auth = credentialEpoch;
    setPreflight(null); setCheckError(""); setChecking(true); setConfirmation(null);
    try {
      const result = await checkEpoch.current.guard(ticket, () => importRequest(`/api/tasks/${selected.id}/preflight`,{method:"POST"}));
      if (!isCredentialCurrent(auth) || selectionRef.current !== key) return;
      if (!validPreflight(result,selected)) throw new Error("预检响应与当前任务不匹配，请重新预检。");
      setPreflight({key,result});
    } catch (error: any) {
      if (error?.name === "AbortError" || !isCredentialCurrent(auth) || selectionRef.current !== key) return;
      setCheckError(describeError(error).message);
    } finally { if (checkEpoch.current.isCurrent(ticket) && isCredentialCurrent(auth) && selectionRef.current === key) setChecking(false); }
  }
  function start() {
    if (!chosenTask || !canStart) return;
    if (!locked && chosenMode === "live" && task && flight && ready) setConfirmation({task,flight,epoch:credentialEpoch});
    else onStart({task_id:chosenTask,mode:chosenMode});
  }

  return <section className="panel new-run" aria-labelledby="new-run-title">
    <h2 id="new-run-title" className="panel-title">修复一个失败测试</h2>
    <p className="small muted">选择任务 → 预检 → 确认调用。标准库 / 固定pytest，小型项目。</p>
    <button type="button" className="button button-block" disabled={locked} onClick={() => setImportOpen(true)}><Icon name="file" size={15}/>导入我的Python项目</button>
    <p className="small muted">选择源码与测试，填写问题。导入不执行代码或调用模型。</p>
    <ImportProjectDialog open={importOpen} onClose={() => setImportOpen(false)} hasToken={Boolean(token)} credentialEpoch={credentialEpoch}
      request={importRequest} health={health} onOperator={onOperator} tokenStatus={tokenStatus} operatorCheck={operatorCheck} onVerifyOperator={onVerifyOperator}
      onImported={(item) => { if(!onImported(item,credentialEpoch))return; invalidate(); setTaskId(item.id); setMode("live"); setImportedNotice({message:"已导入并选中："+item.title+"。先预检，尚未调用模型。",epoch:credentialEpoch}); }}/>
    {token && importedNotice?.epoch===credentialEpoch && <p className="small import-success" role="status">{importedNotice.message}</p>}
    {locked && pending && <div className="pending-box" role="status">
      <p className="pending-title"><Icon name="alert" size={15}/>上次创建请求的结果未确认</p>
      <p>{task?.title ?? pending.body.task_id} · {pending.body.mode === "live" ? "真实模型调用" : "脚本演示"}</p>
      <p className="small">核对会使用原请求编号与内容。服务端若已创建，就返回原运行；不会另建一次调用。</p>
      <details className="small"><summary>请求编号</summary><code>{shortHash(pending.key,8,4)}</code></details>
    </div>}
    <fieldset className="choice-group" disabled={locked || busy}>
      <legend>选择任务 <span className="legend-note">公开样例与自己的私有项目</span></legend>
      {tasksError && <p className="error-line" role="alert">{tasksError}</p>}
      {!tasks && !tasksError && <p className="muted small"><Spinner/> 正在读取任务…</p>}
      {tasks?.length === 0 && <p className="muted small">任务目录为空。</p>}
      {tasks?.map((item) => <label key={item.id} className="task-option">
        <input type="radio" name="task" value={item.id} checked={chosenTask===item.id} onChange={() => { invalidate(); setTaskId(item.id); setImportedNotice(null); }}/>
        <span className="task-card"><span className="task-title">{item.private ? "我的项目 · " : ""}{item.title}</span><span className="task-desc">{item.description}</span>
          <span className="task-meta"><code>{item.editable_files.join(", ")}</code></span></span>
      </label>)}
    </fieldset>
    <fieldset className="choice-group mode-group" disabled={locked || busy}>
      <legend>执行方式</legend>
      <label className="mode-option is-demo"><input type="radio" name="mode" value="demo" disabled={task?.private} checked={chosenMode==="demo"} onChange={() => { setMode("demo"); setConfirmation(null); }}/>
        <span className="mode-card"><ModeTag mode="demo"/><span className="mode-desc">预置动作与修复，不调用模型。体验审阅与测试流程，不代表模型效果。</span></span></label>
      <label className="mode-option is-live"><input type="radio" name="mode" value="live" checked={chosenMode==="live"} onChange={() => setMode("live")}/>
        <span className="mode-card"><ModeTag mode="live"/><span className="mode-desc">先预检，再单独确认。{session?.role === "operator" ? "使用原操作者配置。" : "使用你的DeepSeek Key。"}真实调用可能产生模型费用。</span></span></label>
    </fieldset>
    {task?.private && <p className="small muted">私有快照 · {task.check_profile} · 最多修改{task.editable_files.length}个源码文件。测试和配置保持只读。</p>}
    {blockers.length > 0 && <ul className="blockers">{blockers.map((blocker) => <li key={blocker.text}><Icon name="info" size={14}/><span>{blocker.text}</span>
      {blocker.action === "operator" && <button type="button" className="link-button" onClick={onOperator}>连接访问</button>}
      {blocker.action === "verify" && <button type="button" className="link-button" disabled={operatorCheck.checking} onClick={onVerifyOperator}>重新验证</button>}</li>)}</ul>}
    {chosenMode === "live" && !locked && <div className="preflight-box">
      <h3>先检查原始测试</h3>
      <p className="small muted">只在固定容器中执行测试，不调用模型。不会安装依赖或改动文件。</p>
      <button type="button" className="button button-block" disabled={!canCheck} onClick={() => { void check(); }}>{checking ? <Spinner/> : <Icon name="check" size={15}/>} {checking ? "正在预检，不调用模型…" : flight ? "重新预检" : "运行预检（不调用模型）"}</button>
      {checkError && <p className="error-line" role="alert">{checkError}</p>}
      {flight && <div className={`preflight-result${ready ? " is-ready" : ""}`} role="status">
        <strong>{PREFLIGHT_LABELS[flight.status]}</strong>
        <p className="small">{flight.check.tests_run !== undefined ? `${flight.check.tests_run}条测试 · ` : ""}{flight.check.duration_ms !== undefined ? `${(flight.check.duration_ms / 1000).toFixed(1)}秒 · ` : ""}{flight.check_profile}</p>
        {!flight.eligible && <p className="small">本次不会启动模型。请先调整测试或补齐受支持的运行条件，再重新导入或预检。</p>}
        {flight.eligible && !ready && <p className="inline-warn">预检已过期，请重新检查后确认调用。</p>}
        {flight.check.error && <p className="small">{flight.check.error}</p>}
        {flight.check.output && <details className="output-details" open={!flight.eligible}><summary>查看原始测试输出</summary><div className="output-scroll"><pre>{flight.check.output}</pre></div></details>}
      </div>}
    </div>}
    <button type="button" className={`button button-primary button-block${chosenMode === "live" && !locked ? " is-live" : ""}`} disabled={!canStart} onClick={start} aria-describedby={failure ? "create-error" : undefined}>
      {busy ? <Spinner/> : <Icon name={locked ? "resume" : "play"} size={15}/>}{locked ? "核对上次创建请求" : chosenMode === "live" ? "确认真实模型调用…" : "开始脚本演示"}
    </button>
    {failure && <div id="create-error" className="create-failure"><ErrorLine error={failure}/>
      {failure.outcome === "unknown" && <p className="small">结果未知，已保留同一请求。再次点击核对，不另建运行。</p>}
      {failure.outcome === "needs_operator" && <p className="small">请用原身份重新连接后核对这次请求。</p>}
      {failure.outcome === "key_conflict" && <p className="small">请求身份发生冲突，请先在自己的运行记录中核对。</p>}</div>}
    {session?.role === "member" && health?.member_limits && <p className="small muted">成员额度：同时{health.member_limits.active_runs}个运行，共{health.member_limits.total_runs}个运行、{health.member_limits.snapshots}份项目快照。</p>}
    <details className="small model-help"><summary>数据与费用说明</summary><p>邀请连接、导入、预检都不调用模型。真实调用会把本任务源码、测试和工具结果发送给DeepSeek官方服务；平台不收费，费用由你的模型账户承担。退出后已发出的请求仍可能完成并计费。</p></details>
    <Dialog open={Boolean(confirmation)} onClose={() => setConfirmation(null)} title="确认调用DeepSeek修复？" tone="live" footer={<>
      <button type="button" className="button" onClick={() => setConfirmation(null)} data-autofocus>先不调用</button>
      <button type="button" className="button button-primary is-live" disabled={!confirmationValid || busy} onClick={() => {
        if (!confirmation || !confirmationValid || !isCredentialCurrent(confirmation.epoch) || !canUsePreflight(confirmation.flight,task)) return;
        const selected = confirmation; setConfirmation(null); onStart({task_id:selected.task.id,mode:"live",preflight_id:selected.flight.preflight_id});
      }}><Icon name="spark" size={15}/>确认真实调用</button>
    </>}>
      <dl className="kv"><dt>任务</dt><dd>{confirmation?.task.title}</dd><dt>预检</dt><dd>已确认原始测试失败，尚未调用模型。</dd><dt>模型</dt><dd>{session?.model ?? health?.model ?? "—"}</dd>
        <dt>本次预算</dt><dd>{health?.run_limits ? `最多${health.run_limits.max_calls}轮模型请求、${health.run_limits.max_steps}次工具、${health.run_limits.max_seconds}秒。` : "服务端限制模型轮数、工具次数和总时间；创建后显示实际预算。"}</dd></dl>
      <p>这一步开始真实API调用，费用由你的DeepSeek账户承担。任务源码、测试和工具结果会发送到DeepSeek官方接口。未知结果的请求不会自动重试。</p>
      <p>模型提出补丁后暂停；你审阅并确认后，才会在新副本中运行测试。通过后才能下载修复文件。</p>
      {!confirmationValid && <p className="inline-warn" role="alert">任务、会话或预检状态已变化，请返回重新预检。</p>}
    </Dialog>
  </section>;
}
