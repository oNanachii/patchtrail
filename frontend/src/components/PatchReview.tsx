import { useEffect, useMemo, useState } from "react";
import { parseUnifiedDiff } from "../lib/diff.js";
import { formatClock, parseTime } from "../lib/format.js";
import type { Run, RunEvent } from "../types";
import { DiffView } from "./DiffView";
import { Dialog, ErrorLine, Hash, Icon, Spinner } from "./ui";

export type ApprovalSnapshot = { runId: string; digest: string; version: number; path: string };
type Described = { message: string; code: string; status: number; server?: string; network?: boolean };

export function PatchReview({ run, events, onApprove }: {
  run: Run;
  events: RunEvent[];
  onApprove: (snapshot: ApprovalSnapshot) => Promise<void>;
}) {
  const proposal = run.proposal!;
  const [snapshot, setSnapshot] = useState<ApprovalSnapshot | null>(null);
  const approval = useMemo(() => {
    const event = events.find((e) => e.kind === "state_changed" && e.payload?.from === "awaiting_approval" && e.payload?.to === "verifying");
    return event ? { at: parseTime(event.created_at), version: event.payload.version as number } : null;
  }, [events]);
  const awaiting = run.state === "awaiting_approval";
  const isLive = run.mode === "live";
  const files = proposal.files?.length ? proposal.files : [{ path: proposal.path ?? "", old_sha256: proposal.old_sha256 ?? "", new_sha256: proposal.new_sha256 ?? "", diff: proposal.diff }];
  const paths = files.map((file) => file.path).join(", ");

  return (
    <section className="panel patch-review" aria-labelledby="patch-title" id="patch">
      <header className="section-head">
        <h3 id="patch-title" tabIndex={-1}><Icon name="patch" />补丁审阅</h3>
        {awaiting ? <span className="chip tone-attention">等待你确认</span>
          : approval ? <span className="chip tone-success-soft">已确认 · {formatClock(approval.at)}</span>
          : <span className="chip">未确认</span>}
      </header>

      {proposal.explanation && (
        <div className={`explanation${isLive ? " is-model" : ""}`}>
          <p className="explanation-label">{isLive ? "模型给出的说明 · 未经验证，不代表结果" : "预置演示说明"}</p>
          <p className="explanation-text">{proposal.explanation}</p>
        </div>
      )}

      {files.map((file) => <DiffView key={file.path} diff={file.diff ?? proposal.diff} label={`补丁差异 ${file.path}`} />)}

      <details className="raw">
        <summary>技术详情：补丁身份、源版本与文件哈希</summary>
        <dl className="proof">
        <div className="proof-row proof-digest">
          <dt>补丁 digest</dt>
          <dd><Hash value={proposal.digest} label="补丁 digest" full /></dd>
        </div>
        {files.map((file) => <div className="proof-row" key={file.path}>
          <dt><code>{file.path}</code> 原 → 新</dt>
          <dd className="proof-flow"><Hash value={file.old_sha256} label="原文件 sha256" /><Icon name="chevron" size={12} /><Hash value={file.new_sha256} label="新文件 sha256" /></dd>
        </div>)}
        <div className="proof-row">
          <dt>调查工作区</dt>
          <dd><Hash value={proposal.base_workspace_hash} label="调查工作区哈希" /></dd>
        </div>
        <div className="proof-row">
          <dt>任务源</dt>
          <dd><Hash value={proposal.source_hash} label="任务源哈希" /></dd>
        </div>
        </dl>
      </details>

      {awaiting && (
        <div className="approve-bar">
          <p>
            确认只针对当前这份修改；内容或版本变化时，服务会拒绝旧确认。
            随后会从原始任务重建工作区，在容器里运行原有测试。
          </p>
          <button type="button" className="button button-primary" onClick={() => setSnapshot({ runId: run.id, digest: proposal.digest, version: run.version, path: paths })}>
            <Icon name="check" size={15} />确认修改并验证…
          </button>
        </div>
      )}
      {approval && (
        <p className="small muted approved-note">
          <Icon name="check" size={13} /> 已确认这份修改。确认之后的结论只来自验证器输出；技术详情保留准确的补丁与源版本。
        </p>
      )}

      <ApproveDialog snapshot={snapshot} run={run} onClose={() => setSnapshot(null)} onApprove={onApprove} />
    </section>
  );
}

function ApproveDialog({ snapshot, run, onClose, onApprove }: {
  snapshot: ApprovalSnapshot | null;
  run: Run;
  onClose: () => void;
  onApprove: (snapshot: ApprovalSnapshot) => Promise<void>;
}) {
  const [phase, setPhase] = useState<"idle" | "sending" | "error">("idle");
  const [error, setError] = useState<Described | null>(null);
  const [attempted, setAttempted] = useState(false);
  useEffect(() => {
    if (!snapshot) { setPhase("idle"); setError(null); setAttempted(false); }
  }, [snapshot]);

  const committed = Boolean(snapshot && attempted && run.id === snapshot.runId && run.proposal?.digest === snapshot.digest
    && ["verifying", "succeeded", "failed"].includes(run.state));
  useEffect(() => {
    if (committed) onClose();
  }, [committed, onClose]);

  if (!snapshot) return <Dialog open={false} onClose={onClose} title="">{null}</Dialog>;
  const stale = !attempted && (run.id !== snapshot.runId || run.version !== snapshot.version
    || run.state !== "awaiting_approval" || run.proposal?.digest !== snapshot.digest);
  const stats = (run.proposal?.files ?? [{ diff: run.proposal?.diff ?? "" }]).map((file) => parseUnifiedDiff(file.diff ?? "")).reduce((sum, item) => ({ added: sum.added + item.added, removed: sum.removed + item.removed }), { added: 0, removed: 0 });

  const submit = async () => {
    setPhase("sending");
    setError(null);
    setAttempted(true);
    try {
      await onApprove(snapshot);
      onClose();
    } catch (caught) {
      setError(caught as Described);
      setPhase("error");
    }
  };
  const unknown = error?.network || (error && error.status >= 500);
  const conflict = error?.status === 409;

  return (
    <Dialog open onClose={onClose} title="确认这份修改并运行测试" tone="attention"
      footer={
        <>
          <button type="button" className="button" onClick={onClose} data-autofocus>返回审阅</button>
          {!conflict && (
            <button type="button" className="button button-primary" onClick={submit} disabled={stale || phase === "sending"}>
              {phase === "sending" ? <Spinner /> : <Icon name="check" size={15} />}
              {unknown ? "用同一确认重试" : "确认并运行测试"}
            </button>
          )}
        </>
      }>
      <dl className="kv">
        <dt>文件</dt><dd><code>{snapshot.path}</code> <span className="diff-add-count">+{stats.added}</span> <span className="diff-del-count">−{stats.removed}</span></dd>
      </dl>
      <details className="raw">
        <summary>查看本次确认绑定的补丁与版本</summary>
        <dl className="kv">
          <dt>proposal_digest</dt><dd><Hash value={snapshot.digest} label="补丁 digest" full /></dd>
          <dt>expected_version</dt><dd><code>{snapshot.version}</code></dd>
        </dl>
      </details>
      <ol className="plain-list numbered small">
        <li>从原始任务创建独立工作区，只应用你正在审阅的这份修改。</li>
        <li>在隔离容器中运行原有测试；源码或版本不符会拒绝验证。</li>
        <li>验证通过后可下载修复文件包，再由你应用到自己的项目。</li>
      </ol>
      {stale && (
        <p className="inline-warn" role="alert">
          <Icon name="alert" size={14} />
          运行在你打开确认后发生了变化（现在是 {run.state} · v{run.version}）。请返回重新审阅。
        </p>
      )}
      {error && <ErrorLine error={error} />}
      {unknown && <p className="small">确认结果未知。服务端对同一 digest 与版本的确认是幂等的，可以安全重试；页面也会继续同步状态。</p>}
      {conflict && <p className="small">服务端拒绝了这次确认：补丁或版本已不是待确认状态。页面已重新获取最新状态，请返回重新审阅。</p>}
    </Dialog>
  );
}
