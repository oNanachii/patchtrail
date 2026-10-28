import { useEffect, useRef, useState } from "react";
import { isCheckResult } from "../lib/checks.js";
import { formatCount, formatDateTime, formatDuration, formatRelative, parseTime, shortHash } from "../lib/format.js";
import { runStopReason, TERMINAL } from "../lib/labels.js";
import { findCreated } from "../lib/trail.js";
import type { Health, Run, RunEvent, Task } from "../types";
import { CheckPanel } from "./CheckPanel";
import { type ApprovalSnapshot, PatchReview } from "./PatchReview";
import { StageTrack } from "./StageTrack";
import { Trail } from "./Trail";
import { Callout, CopyButton, Dialog, ErrorLine, Icon, ModeTag, Spinner, StateBadge } from "./ui";

type Described = { message: string; code: string; status: number; server?: string; network?: boolean };

export type RunActions = {
  approve: (snapshot: ApprovalSnapshot) => Promise<void>;
  cancel: (run: Run) => Promise<void>;
  resume: (run: Run, version: number) => Promise<void>;
  download: (run: Run) => Promise<void>;
};

export function RunDetail({ run, events, error, tasks, health, now, actions, focusKey }: {
  run: Run;
  events: RunEvent[];
  error: Described | null;
  tasks: Task[] | null;
  health: Health | null;
  now: number;
  actions: RunActions;
  focusKey: number;
}) {
  const task = tasks?.find((t) => t.id === run.task_id) ?? null;
  const headingRef = useRef<HTMLHeadingElement>(null);
  const [dialog, setDialog] = useState<null | "cancel" | "resume">(null);
  const [download, setDownload] = useState<{ busy: boolean; error: Described | null }>({ busy: false, error: null });
  useEffect(() => {
    if (focusKey) headingRef.current?.focus();
  }, [focusKey]);

  const created = parseTime(run.created_at);
  const updated = parseTime(run.updated_at);
  const cancellable = !TERMINAL.has(run.state);

  return (
    <article className="run-detail" aria-labelledby="run-heading">
      <header className="run-head">
        <div className="run-head-main">
          <div className="run-head-tags">
            <ModeTag mode={run.mode} />
            <StateBadge run={run} large />
          </div>
          <h2 id="run-heading" ref={headingRef} tabIndex={-1}>{task?.title ?? run.task_id}</h2>
          <p className="run-meta">
            <span className="run-id"><code>{run.id}</code><CopyButton value={run.id} label="运行 ID" /></span>
            <span>版本 <code>v{run.version}</code></span>
            <span>创建 <time dateTime={run.created_at} title={formatDateTime(created)}>{formatRelative(created, now)}</time></span>
            <span>更新 <time dateTime={run.updated_at} title={formatDateTime(updated)}>{formatRelative(updated, now)}</time></span>
          </p>
        </div>
        <div className="run-head-actions">
          {run.state === "interrupted" && (
            <button type="button" className="button button-primary" onClick={() => setDialog("resume")}>
              <Icon name="resume" size={15} />明确恢复…
            </button>
          )}
          {cancellable && (
            <button type="button" className="button button-danger-quiet" onClick={() => setDialog("cancel")}>
              <Icon name="stop" size={14} />取消运行…
            </button>
          )}
          <button type="button" className="button" disabled={download.busy}
            onClick={async () => {
              setDownload({ busy: true, error: null });
              try {
                await actions.download(run);
                setDownload({ busy: false, error: null });
              } catch (caught) {
                setDownload({ busy: false, error: caught as Described });
              }
            }}>
            {download.busy ? <Spinner /> : <Icon name="download" size={15} />}下载报告
          </button>
        </div>
      </header>
      {download.error && <ErrorLine error={download.error} />}
      {error && <div className="sync-warn"><ErrorLine error={error} /><span className="small muted">显示的是上次同步的状态。</span></div>}

      <ModeBanner run={run} events={events} health={health} />
      <StageTrack run={run} events={events} now={now} />
      <NextAction run={run} onResume={() => setDialog("resume")} />

      <div className="run-grid">
        <div className="run-col-review">
          {run.proposal ? <PatchReview run={run} events={events} onApprove={actions.approve} /> : (
            <section className="panel patch-review is-empty" aria-labelledby="patch-title">
              <header className="section-head"><h3 id="patch-title"><Icon name="patch" />补丁审阅</h3></header>
              <p className="muted small">
                {TERMINAL.has(run.state) || run.state === "interrupted" ? "这次运行没有提出补丁。" : "尚未提出补丁。Agent 提出单文件补丁后会停在这里，等待你按 digest 与版本确认。"}
              </p>
            </section>
          )}
          <CheckPanel run={run} events={events} />
        </div>
        <div className="run-col-trail">
          <Budget run={run} />
          <TaskBrief task={task} events={events} />
          <Trail run={run} events={events} />
        </div>
      </div>

      <CancelDialog open={dialog === "cancel"} run={run} onClose={() => setDialog(null)} onCancel={actions.cancel} />
      <ResumeDialog open={dialog === "resume"} run={run} onClose={() => setDialog(null)} onResume={actions.resume} />
    </article>
  );
}

function ModeBanner({ run, events, health }: { run: Run; events: RunEvent[]; health: Health | null }) {
  const created = findCreated(events);
  if (run.mode === "demo") {
    return (
      <div className="mode-banner is-demo">
        <Icon name="script" size={16} />
        <p><strong>脚本演示</strong>：预设动作，没有调用模型。补丁是预置修复，用来演示运行时和验证器，不代表模型能力。{run.provider_label && <span className="mono-note"> {run.provider_label}</span>}</p>
      </div>
    );
  }
  const configured = run.configured_model ?? created?.provider ?? health?.model ?? "—";
  const observed = run.observed_models?.length ? run.observed_models.join(", ") : null;
  const usage = run.checkpoint.usage ?? {};
  return (
    <div className="mode-banner is-live">
      <Icon name="spark" size={16} />
      <p>
        <strong>DeepSeek 真实调用</strong>：配置模型 <code>{configured}</code>
        {observed ? <> · API 返回模型 <code>{observed}</code></> : " · 尚无返回模型记录"}
        {" · "}Token {formatCount(usage.total_tokens)}。模型文字与补丁说明未经验证，成功只由验证器判定。
      </p>
    </div>
  );
}

function NextAction({ run, onResume }: { run: Run; onResume: () => void }) {
  const limits = run.limits ?? {};
  const reason = runStopReason(run);
  const result = run.result;
  switch (run.state) {
    case "queued":
      return <Callout tone="neutral" icon="clock" title="排队中：等待单一 worker 开始调查">首版只有一个执行 worker，运行按顺序处理。</Callout>;
    case "diagnosing":
      return (
        <Callout tone="info" icon="search" title="调查中：读取代码、复现失败、准备补丁">
          预算上限：{limits.max_calls ?? "—"} 轮请求、{limits.max_steps ?? "—"} 次工具、{limits.max_seconds ?? "—"} 秒。提出补丁后会自动停下，等待确认。
        </Callout>
      );
    case "awaiting_approval":
      return (
        <Callout tone="attention" icon="gate" title="需要你：审阅补丁，再按 digest 与版本确认"
          actions={
            <button type="button" className="button button-primary" onClick={() => {
              const target = document.getElementById("patch-title");
              target?.scrollIntoView({ behavior: "smooth", block: "start" });
              target?.focus({ preventScroll: true });
            }}><Icon name="patch" size={15} />前往补丁</button>
          }>
          补丁还没有被应用或执行。确认后才会在新工作区的容器中运行测试。
        </Callout>
      );
    case "verifying":
      return <Callout tone="info" icon="shield" title="验证中：在新工作区应用已确认的补丁并运行测试">从不可变任务重建工作区，不复用调查工作区。</Callout>;
    case "succeeded": {
      const r = isCheckResult(result) ? (result as { tests_run?: number; runner: string; duration_ms: number }) : null;
      return (
        <Callout tone="success" icon="check" title="验证通过：不可修改的测试在新工作区中全部通过">
          {r && <>{r.tests_run ?? "—"} 个用例 · exit 0 · {formatDuration(r.duration_ms)} · 执行器 <code>{r.runner}</code>。</>}
          {run.mode === "demo" && " 这是预置补丁，证明的是运行时与验证器可用，不是模型能力。"}
        </Callout>
      );
    }
    case "failed":
      return (
        <Callout tone={reason?.notExecuted || reason?.unconfirmed ? "warning" : "danger"} icon={reason?.notExecuted || reason?.unconfirmed ? "alert" : "x"} title={reason?.title ?? "运行失败"}>
          {reason?.detail ?? "请查看检查结果和执行记录。"}
        </Callout>
      );
    case "cancelled":
      return <Callout tone="muted" icon="stop" title="已取消">取消是终态，不能恢复。可以用同一任务新建一次运行。</Callout>;
    case "interrupted": {
      const unknown = run.checkpoint.unconfirmed_model_request;
      return (
        <Callout tone="warning" icon="alert" title={reason?.title ?? "运行已中断"}
          actions={<button type="button" className="button button-primary" onClick={onResume}><Icon name="resume" size={15} />明确恢复…</button>}>
          {run.proposal ? "补丁此前已确认；恢复会在新工作区重新验证同一 digest。" : "恢复会从检查点重新排队调查。"}
          {unknown && <strong className="block-warn">上一次{run.mode === "live" ? "模型" : "脚本"}请求的结果未知。服务端不会自动重放；恢复后会发起新的请求{run.mode === "live" ? "，可能产生额外费用" : ""}。</strong>}
        </Callout>
      );
    }
    default:
      return null;
  }
}

function Meter({ label, value, max, format }: { label: string; value: number | undefined; max: number | undefined; format?: (n: number) => string }) {
  const v = value ?? 0;
  const ratio = max ? Math.min(1, v / max) : 0;
  const show = format ?? ((n: number) => String(n));
  return (
    <div className="meter">
      <div className="meter-top"><span>{label}</span><span className="mono">{show(v)}{max != null ? ` / ${show(max)}` : ""}</span></div>
      <div className={`meter-bar${ratio >= 1 ? " is-full" : ""}`} role="meter" aria-label={label} aria-valuemin={0} aria-valuemax={max ?? v} aria-valuenow={v}>
        <span className="meter-fill" style={{ width: `${ratio * 100}%` }} />
      </div>
    </div>
  );
}

function Budget({ run }: { run: Run }) {
  const cp = run.checkpoint ?? {};
  const limits = run.limits ?? {};
  const usage = cp.usage ?? {};
  return (
    <section className="panel budget" aria-labelledby="budget-title">
      <header className="section-head"><h3 id="budget-title"><Icon name="clock" />预算与用量</h3></header>
      <div className="meters">
        <Meter label={run.mode === "demo" ? "脚本轮次" : "模型请求"} value={cp.calls} max={limits.max_calls} />
        <Meter label="工具调用" value={cp.steps} max={limits.max_steps} />
        <Meter label="调查耗时" value={(cp.elapsed_ms ?? 0) / 1000} max={limits.max_seconds} format={(n) => `${n.toFixed(n < 10 ? 1 : 0)}s`} />
      </div>
      <p className="small token-line">
        {run.mode === "demo" ? "Token：未调用模型" : (
          <>Token 合计 <code>{formatCount(usage.total_tokens)}</code> · 输入 <code>{formatCount(usage.prompt_tokens)}</code> · 输出 <code>{formatCount(usage.completion_tokens)}</code>
            {usage.prompt_cache_hit_tokens != null && <> · 缓存命中 <code>{formatCount(usage.prompt_cache_hit_tokens)}</code></>}</>
        )}
      </p>
    </section>
  );
}

function TaskBrief({ task, events }: { task: Task | null; events: RunEvent[] }) {
  const created = findCreated(events);
  if (!task) return null;
  const drift = created?.task_version && created.task_version !== task.source_hash;
  return (
    <section className="panel task-brief" aria-labelledby="task-title">
      <header className="section-head"><h3 id="task-title"><Icon name="file" />任务</h3></header>
      <p>{task.description}</p>
      <dl className="kv compact">
        <dt>可编辑</dt><dd><code>{task.editable_files.join(", ")}</code></dd>
        <dt>检查</dt><dd><code>{task.check_id}</code> · 不可修改的 unittest</dd>
        <dt>任务源</dt><dd><code title={task.source_hash}>{shortHash(task.source_hash)}</code></dd>
      </dl>
      {drift && <p className="inline-warn"><Icon name="alert" size={14} />创建时的任务版本 {shortHash(created.task_version)} 与当前目录不同。</p>}
    </section>
  );
}

function CancelDialog({ open, run, onClose, onCancel }: { open: boolean; run: Run; onClose: () => void; onCancel: (run: Run) => Promise<void> }) {
  const [state, setState] = useState<{ busy: boolean; error: Described | null }>({ busy: false, error: null });
  useEffect(() => { if (!open) setState({ busy: false, error: null }); }, [open]);
  useEffect(() => { if (open && TERMINAL.has(run.state)) onClose(); }, [open, run.state, onClose]);
  return (
    <Dialog open={open} onClose={onClose} title="取消这次运行？" tone="danger"
      footer={
        <>
          <button type="button" className="button" onClick={onClose} data-autofocus>保留运行</button>
          <button type="button" className="button button-danger" disabled={state.busy}
            onClick={async () => {
              setState({ busy: true, error: null });
              try { await onCancel(run); onClose(); } catch (caught) { setState({ busy: false, error: caught as Described }); }
            }}>
            {state.busy ? <Spinner /> : <Icon name="stop" size={14} />}取消运行
          </button>
        </>
      }>
      <p>取消是终态，不能恢复。当前状态：{run.state} · v{run.version}。</p>
      <p className="small muted">进行中的单次模型请求或容器检查可能会继续到结束，但它的结果不会改变已取消的状态。</p>
      {state.error && <ErrorLine error={state.error} />}
    </Dialog>
  );
}

function ResumeDialog({ open, run, onClose, onResume }: { open: boolean; run: Run; onClose: () => void; onResume: (run: Run, version: number) => Promise<void> }) {
  const [snapshot, setSnapshot] = useState<number | null>(null);
  const [state, setState] = useState<{ busy: boolean; error: Described | null }>({ busy: false, error: null });
  useEffect(() => {
    if (open) setSnapshot(run.version);
    else { setSnapshot(null); setState({ busy: false, error: null }); }
    // Capture the version once, when the dialog opens.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);
  const stale = snapshot != null && (run.version !== snapshot || run.state !== "interrupted") && !state.busy;
  const unknown = run.checkpoint.unconfirmed_model_request;
  return (
    <Dialog open={open} onClose={onClose} title="明确恢复这次运行？" tone="warning"
      footer={
        <>
          <button type="button" className="button" onClick={onClose} data-autofocus>返回</button>
          <button type="button" className="button button-primary" disabled={stale || state.busy || snapshot == null}
            onClick={async () => {
              if (snapshot == null) return;
              setState({ busy: true, error: null });
              try { await onResume(run, snapshot); onClose(); } catch (caught) { setState({ busy: false, error: caught as Described }); }
            }}>
            {state.busy ? <Spinner /> : <Icon name="resume" size={15} />}恢复 · v{snapshot ?? run.version}
          </button>
        </>
      }>
      <p>{run.proposal ? "将从不可变任务重建新工作区，重新验证已确认的同一 digest。" : "将把运行重新排队，从已保存的检查点继续调查。"}</p>
      {unknown && (
        <p className="inline-warn">
          <Icon name="alert" size={14} />
          上一次{run.mode === "live" ? "模型" : "脚本"}请求的结果未知。服务端不会重放它；恢复后会发起新的请求{run.mode === "live" ? "，可能产生额外的 DeepSeek 费用" : ""}。
        </p>
      )}
      <p className="small muted">恢复请求携带 expected_version = {snapshot ?? "—"}；若状态已变化，服务端会拒绝。</p>
      {stale && <p className="inline-warn" role="alert"><Icon name="alert" size={14} />运行状态已变化（现在 {run.state} · v{run.version}），请关闭后重新查看。</p>}
      {state.error && <ErrorLine error={state.error} />}
    </Dialog>
  );
}
