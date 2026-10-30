import { formatDateTime, formatRelative, parseTime } from "../lib/format.js";
import type { Run, Task, TokenStatus } from "../types";
import { ErrorLine, ModeTag, Spinner, StateBadge } from "./ui";

export function RunList({ runs, error, tasks, selectedId, onSelect, tokenStatus, publicDemo, now, onOperator }: {
  runs: Run[] | null;
  error: { message: string; code: string; status: number } | null;
  tasks: Task[] | null;
  selectedId: string | null;
  onSelect: (id: string) => void;
  tokenStatus: TokenStatus;
  publicDemo: boolean | undefined;
  now: number;
  onOperator: () => void;
}) {
  const operator = tokenStatus === "accepted" || tokenStatus === "unverified";
  return (
    <section className="panel run-list" aria-labelledby="runs-title">
      <div className="panel-head">
        <h2 id="runs-title" className="panel-title">最近运行</h2>
        {runs && <span className="count">{runs.length}</span>}
      </div>
      {error && error.status === 401 ? (
        <div className="empty-inline">
          <p>此服务未开放公开演示，查看运行需要操作令牌。</p>
          <button type="button" className="button" onClick={onOperator}>连接访问</button>
        </div>
      ) : error && !runs ? (
        <ErrorLine error={error} />
      ) : !runs ? (
        <p className="muted small"><Spinner /> 正在读取运行记录…</p>
      ) : runs.length === 0 ? (
        <div className="empty-inline">
          <p>还没有运行记录。</p>
          <p className="small muted">选择一个任务开始。{publicDemo && !operator ? "匿名访问只列出脚本演示运行。" : ""}</p>
        </div>
      ) : (
        <>
          {publicDemo && !operator && <p className="small muted list-note">匿名访问只列出脚本演示运行；连接后显示自己的真实运行。</p>}
          <ol className="runs">
            {runs.map((run) => {
              const created = parseTime(run.created_at);
              const title = tasks?.find((task) => task.id === run.task_id)?.title ?? run.task_id;
              return (
                <li key={run.id}>
                  <a className={`run-row${selectedId === run.id ? " is-selected" : ""}`} href={`#run=${run.id}`}
                    aria-current={selectedId === run.id ? "true" : undefined}
                    onClick={(event) => { event.preventDefault(); onSelect(run.id); }}>
                    <span className="run-row-top">
                      <span className="run-row-title">{title}</span>
                      <StateBadge run={run} />
                    </span>
                    <span className="run-row-meta">
                      <ModeTag mode={run.mode} compact />
                      <code>{run.id.slice(0, 8)}</code>
                      <time dateTime={run.created_at} title={formatDateTime(created)}>{formatRelative(created, now)}</time>
                    </span>
                  </a>
                </li>
              );
            })}
          </ol>
        </>
      )}
      {error && runs && <p className="small muted stale-note">列表暂时无法刷新，显示的是上次同步结果。</p>}
    </section>
  );
}
