import { useEffect, useState } from "react";
import type { PendingCreate } from "../lib/createGuard.js";
import { shortHash } from "../lib/format.js";
import type { Health, Task, TokenStatus } from "../types";
import { Dialog, ErrorLine, Icon, ModeTag, Spinner } from "./ui";

type Mode = "demo" | "live";
export type CreateFailure = { message: string; code: string; status: number; server?: string; outcome: string };

export function NewRun({ tasks, tasksError, health, online, token, tokenStatus, pending, busy, failure, onStart, onOperator }: {
  tasks: Task[] | null;
  tasksError: string | null;
  health: Health | null;
  online: boolean;
  token: string;
  tokenStatus: TokenStatus;
  pending: PendingCreate | null;
  busy: boolean;
  failure: CreateFailure | null;
  onStart: (selection: { task_id: string; mode: Mode }) => void;
  onOperator: () => void;
}) {
  const [taskId, setTaskId] = useState<string | null>(null);
  const [mode, setMode] = useState<Mode>("demo");
  const [confirmLive, setConfirmLive] = useState(false);
  useEffect(() => {
    if (tasks?.length && !tasks.some((task) => task.id === taskId)) setTaskId(tasks[0].id);
  }, [tasks, taskId]);

  const locked = Boolean(pending);
  const chosenTask = locked ? pending!.body.task_id : taskId;
  const task = tasks?.find((t) => t.id === chosenTask) ?? null;
  const chosenMode: Mode = locked ? pending!.body.mode : task?.private ? "live" : mode;

  const blockers: { text: string; action?: "operator" }[] = [];
  if (!locked) {
    if (!online) blockers.push({ text: "服务失联，暂时不能创建运行。" });
    else if (health && !health.container_available) blockers.push({ text: "容器运行环境不可用。服务不会转到宿主机执行，所以现在无法创建运行；已有记录仍可查看。" });
    if (chosenMode === "live" && health) {
      if (!health.model_configured) blockers.push({ text: "服务端没有配置 DeepSeek 密钥。" });
      if (!health.operator_configured) blockers.push({ text: "服务端没有配置操作令牌，live 功能不可用。" });
      else if (!token) blockers.push({ text: "live 运行需要操作令牌。", action: "operator" });
      else if (tokenStatus === "rejected") blockers.push({ text: "当前令牌被拒绝，请重新载入。", action: "operator" });
    }
    if (!health && online) blockers.push({ text: "正在读取服务状态…" });
  }
  const canStart = !busy && Boolean(chosenTask) && blockers.length === 0;

  const start = () => {
    if (!chosenTask) return;
    if (!locked && chosenMode === "live") setConfirmLive(true);
    else onStart({ task_id: chosenTask, mode: chosenMode });
  };

  return (
    <section className="panel new-run" aria-labelledby="new-run-title">
      <h2 id="new-run-title" className="panel-title">新建运行</h2>
      {locked && pending && (
        <div className="pending-box" role="status">
          <p className="pending-title"><Icon name="alert" size={15} />上次创建请求的结果未确认</p>
          <p>
            {task?.title ?? pending.body.task_id} · {pending.body.mode === "live" ? "DeepSeek 真实调用" : "脚本演示"} · 幂等键 <code>{shortHash(pending.key, 8, 4)}</code>
          </p>
          <p className="small">再次提交会使用同一个幂等键和请求体。若服务端已经创建，会直接返回原运行，不会重复创建{pending.body.mode === "live" ? "或重复计费" : ""}。</p>
          {pending.body.mode === "live" && !token && (
            <p className="small">核对 live 请求需要操作令牌。<button type="button" className="link-button" onClick={onOperator}>载入令牌</button></p>
          )}
        </div>
      )}

      <fieldset className="choice-group" disabled={locked}>
        <legend>任务 <span className="legend-note">公开演示与操作者导入的私有快照</span></legend>
        {tasksError && <p className="error-line" role="alert"><Icon name="alert" size={14} />{tasksError}</p>}
        {!tasks && !tasksError && <p className="muted small"><Spinner /> 正在读取任务目录…</p>}
        {tasks?.length === 0 && <p className="muted small">任务目录为空。</p>}
        {tasks?.map((item) => (
          <label key={item.id} className="task-option">
            <input type="radio" name="task" value={item.id} checked={chosenTask === item.id} onChange={() => setTaskId(item.id)} />
            <span className="task-card">
              <span className="task-title">{item.private ? "私有项目 · " : ""}{item.title}</span>
              <span className="task-desc">{item.description}</span>
              <span className="task-meta">
                <code>{item.editable_files.join(", ")}</code>
                <span className="task-tags">{item.tags.map((tag) => <span key={tag}>{tag}</span>)}</span>
              </span>
            </span>
          </label>
        ))}
      </fieldset>

      <fieldset className="choice-group mode-group" disabled={locked}>
        <legend>执行方式</legend>
        <label className="mode-option is-demo">
          <input type="radio" name="mode" value="demo" disabled={task?.private} checked={chosenMode === "demo"} onChange={() => setMode("demo")} />
          <span className="mode-card">
            <ModeTag mode="demo" />
            <span className="mode-desc">预设动作，不调用模型。补丁是预置修复，用来演示运行时和验证器，不代表模型能力。</span>
          </span>
        </label>
        <label className="mode-option is-live">
          <input type="radio" name="mode" value="live" checked={chosenMode === "live"} onChange={() => setMode("live")} />
          <span className="mode-card">
            <ModeTag mode="live" />
            <span className="mode-desc">
              真实 API 调用，可能产生费用；需要操作令牌。
              {health && (health.model_configured ? <> 模型 <code>{health.model}</code>。</> : " 服务端未配置密钥。")}
            </span>
          </span>
        </label>
      </fieldset>

      {task?.private && <p className="small muted">本地导入的不可变快照 · {task.check_profile} · {task.editable_files.length}个可修改文件。源码、记录和补丁仅操作者可见。</p>}

      {blockers.length > 0 && (
        <ul className="blockers">
          {blockers.map((blocker) => (
            <li key={blocker.text}>
              <Icon name="info" size={14} />
              <span>{blocker.text}</span>
              {blocker.action === "operator" && <button type="button" className="link-button" onClick={onOperator}>载入令牌</button>}
            </li>
          ))}
        </ul>
      )}

      <button type="button" className={`button button-primary button-block${chosenMode === "live" && !locked ? " is-live" : ""}`}
        disabled={!canStart} onClick={start} aria-describedby={failure ? "create-error" : undefined}>
        {busy ? <Spinner /> : <Icon name={locked ? "resume" : "play"} size={15} />}
        {locked ? "核对上次创建请求" : chosenMode === "live" ? "开始真实调用…" : "开始脚本演示"}
      </button>
      {failure && (
        <div id="create-error" className="create-failure">
          <ErrorLine error={failure} />
          {failure.outcome === "unknown" && <p className="small">请求结果未知，已保留同一请求。再次点击会核对，不会重复创建。</p>}
          {failure.outcome === "needs_operator" && <p className="small">已保留同一请求；载入令牌后再核对。</p>}
          {failure.outcome === "key_conflict" && <p className="small">这个幂等键已被另一请求使用。请先在最近运行中核对，再新建。</p>}
        </div>
      )}

      <Dialog open={confirmLive} onClose={() => setConfirmLive(false)} title="开始真实模型运行？" tone="live"
        footer={
          <>
            <button type="button" className="button" onClick={() => setConfirmLive(false)} data-autofocus>取消</button>
            <button type="button" className="button button-primary is-live" onClick={() => { setConfirmLive(false); if (chosenTask) onStart({ task_id: chosenTask, mode: "live" }); }}>
              <Icon name="spark" size={15} />调用 DeepSeek
            </button>
          </>
        }>
        <dl className="kv">
          <dt>任务</dt><dd>{task?.title ?? chosenTask}</dd>
          <dt>模型</dt><dd><code>{health?.model ?? "—"}</code></dd>
          <dt>执行</dt><dd>单 Agent，有界调用、工具与时间预算（服务端配置）</dd>
        </dl>
        <ul className="plain-list small">
          <li>会产生真实 API 调用和费用。未知结果的请求不会自动重试。</li>
          <li>模型只能提出补丁；应用前需要你确认准确的 digest 与版本。</li>
          <li>模型的文字说明不算结果；成功只由容器中的不可修改测试判定。</li>
        </ul>
      </Dialog>
    </section>
  );
}
