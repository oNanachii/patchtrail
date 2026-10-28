import { useMemo } from "react";
import { formatDuration, shortHash } from "../lib/format.js";
import { STATES, TOOL_LABELS } from "../lib/labels.js";
import { buildTrail, type Round, type ToolCall, type TrailItem } from "../lib/trail.js";
import type { Run, RunEvent } from "../types";
import { OutputBlock, RunnerTag } from "./CheckPanel";
import { Icon, type IconName } from "./ui";

const TOOL_ICONS: Record<string, IconName> = { read_file: "file", search_code: "search", run_check: "terminal", propose_patch: "patch" };

export function Trail({ run, events }: { run: Run; events: RunEvent[] }) {
  const { items, checkpoints } = useMemo(() => buildTrail(events), [events]);
  const origin = items[0]?.at ?? null;
  return (
    <section className="panel trail-panel" aria-labelledby="trail-title" id="trail">
      <header className="section-head">
        <h3 id="trail-title"><Icon name="flag" />执行记录</h3>
        <span className="small muted">{events.length} 条事件</span>
      </header>
      {items.length === 0 ? (
        <p className="muted small">还没有事件。</p>
      ) : (
        <ol className="trail" aria-live="off">
          {items.map((item) => <TrailEntry key={`${item.kind}-${item.id}`} item={item} run={run} origin={origin} />)}
        </ol>
      )}
      {checkpoints > 0 && <p className="small muted trail-foot">另有 {checkpoints} 次同状态检查点保存未单独列出（完整记录见报告）。</p>}
    </section>
  );
}

function Offset({ at, origin }: { at: number | null; origin: number | null }) {
  if (at == null || origin == null) return null;
  return <span className="trail-time">+{formatDuration(Math.max(0, at - origin))}</span>;
}

function TrailEntry({ item, run, origin }: { item: TrailItem; run: Run; origin: number | null }) {
  switch (item.kind) {
    case "created":
      return (
        <li className="trail-item trail-marker">
          <span className="trail-node" aria-hidden="true" />
          <div className="trail-body">
            <p className="trail-title">运行已创建 <Offset at={item.at} origin={origin} /></p>
            <p className="trail-sub">
              {item.payload.mode === "live" ? <>模型 <code>{item.payload.provider}</code></> : "脚本演示 · 不调用模型"}
              {item.payload.task_version && <> · 任务版本 <code title={item.payload.task_version}>{shortHash(item.payload.task_version)}</code></>}
              {item.payload.runtime_code_hash && <> · 运行时 <code title={item.payload.runtime_code_hash}>{shortHash(item.payload.runtime_code_hash)}</code></>}
            </p>
          </div>
        </li>
      );
    case "stage": {
      const to = STATES[item.payload.to];
      return (
        <li className={`trail-item trail-stage tone-${to?.tone ?? "neutral"}`}>
          <span className="trail-node" aria-hidden="true" />
          <div className="trail-body">
            <p className="trail-title">
              状态 → {to?.label ?? item.payload.to} <code className="trail-version">v{item.payload.version}</code>
              <Offset at={item.at} origin={origin} />
            </p>
            {item.payload.reason && <p className="trail-sub">原因 <code>{item.payload.reason}</code></p>}
          </div>
        </li>
      );
    }
    case "round":
      return <RoundEntry round={item} run={run} origin={origin} />;
    case "tool":
      return (
        <li className="trail-item trail-round">
          <span className="trail-node" aria-hidden="true" />
          <div className="trail-body"><ToolEntry tool={item.tool} /></div>
        </li>
      );
    case "verification":
      return (
        <li className="trail-item trail-marker tone-info">
          <span className="trail-node" aria-hidden="true" />
          <div className="trail-body">
            <p className="trail-title">开始独立验证 <Offset at={item.at} origin={origin} /></p>
            <p className="trail-sub">
              新工作区 <code title={item.payload.workspace_hash}>{shortHash(item.payload.workspace_hash)}</code> · 补丁 <code title={item.payload.proposal_digest}>{shortHash(item.payload.proposal_digest)}</code>
              {item.payload.runner && <> · {item.payload.runner}</>}
            </p>
          </div>
        </li>
      );
    case "check_unconfirmed":
      return (
        <li className="trail-item trail-marker tone-warning">
          <span className="trail-node" aria-hidden="true" />
          <div className="trail-body">
            <p className="trail-title">基线检查未能确认 <Offset at={item.at} origin={origin} /></p>
            <p className="trail-sub">
              {item.payload.tests_run ?? 0} 个用例 · exit {item.payload.exit_code ?? "—"}{item.payload.timed_out ? " · 超时" : ""}{item.payload.truncated ? " · 输出截断" : ""}
            </p>
            {typeof item.payload.output === "string" && item.payload.output && <OutputBlock output={item.payload.output} label="未确认的检查输出" />}
          </div>
        </li>
      );
    case "resume_unknown":
      return (
        <li className="trail-item trail-marker tone-warning">
          <span className="trail-node" aria-hidden="true" />
          <div className="trail-body">
            <p className="trail-title">明确恢复：上一次请求结果未知 <Offset at={item.at} origin={origin} /></p>
            <p className="trail-sub">请求 <code>{String(item.payload.previous_request ?? "—").slice(0, 12)}</code> 没有被自动重放；恢复后发起的是新请求。</p>
          </div>
        </li>
      );
    default:
      return (
        <li className="trail-item trail-marker">
          <span className="trail-node" aria-hidden="true" />
          <div className="trail-body">
            <p className="trail-title"><code>{item.event.kind}</code> <Offset at={item.at} origin={origin} /></p>
            <details className="raw"><summary>原始事件</summary><pre className="code-block">{JSON.stringify(item.payload, null, 2)}</pre></details>
          </div>
        </li>
      );
  }
}

function RoundEntry({ round, run, origin }: { round: Round; run: Run; origin: number | null }) {
  const scripted = run.mode === "demo";
  const response = round.response;
  return (
    <li className={`trail-item trail-round${scripted ? " is-scripted" : " is-live"}`}>
      <span className="trail-node" aria-hidden="true"><span>{round.call}</span></span>
      <div className="trail-body">
        <p className="trail-title">
          {scripted ? `脚本动作 #${round.call}` : `DeepSeek 请求 #${round.call}`}
          <span className="trail-tag">{scripted ? "未调用模型" : "真实调用"}</span>
          <Offset at={round.at} origin={origin} />
        </p>
        {response && (
          <p className="trail-sub">
            {Array.isArray(response.tool_names) && response.tool_names.length > 0 ? <>请求工具 {response.tool_names.map((n: string, index: number) => <code key={`${n}:${index}`}>{n}</code>)}</> : "没有请求工具"}
            {response.reported_model && <> · 返回模型 <code>{response.reported_model}</code></>}
            {response.finish_reason && <> · {response.finish_reason}</>}
          </p>
        )}
        {response?.content && (
          <blockquote className="model-text">
            <p className="model-text-label">模型文本 · 未经验证，不是结果{response.content_truncated ? " · 已截断" : ""}</p>
            <p>{response.content}</p>
          </blockquote>
        )}
        {round.tools.length > 0 && (
          <ul className="tools">
            {round.tools.map((tool) => <li key={tool.key}><ToolEntry tool={tool} /></li>)}
          </ul>
        )}
      </div>
    </li>
  );
}

function toolSummary(tool: ToolCall) {
  const a = tool.args ?? {};
  const r = tool.result ?? {};
  switch (tool.name) {
    case "read_file":
      return <><code>{r.path ?? a.path}</code>{r.start != null && <span> L{r.start}–{r.end}</span>}{r.truncated && <span> · 截断</span>}</>;
    case "search_code":
      return <><code>“{a.query ?? r.query}”</code>{Array.isArray(r.hits) && <span> · {r.hits.length} 处{r.truncated ? "+" : ""}</span>}</>;
    case "run_check":
      return r.exit_code !== undefined
        ? <><code>{a.check_id ?? "tests"}</code><span> · exit {r.exit_code ?? "—"} · {r.tests_run ?? "?"} 个用例 · {formatDuration(r.duration_ms)}</span></>
        : <code>{a.check_id ?? "tests"}</code>;
    case "propose_patch":
      return <><code>{a.path}</code>{a.content && <span> · {a.content}</span>}</>;
    default:
      return null;
  }
}

function ToolEntry({ tool }: { tool: ToolCall }) {
  const failed = Boolean(tool.error);
  const pending = !tool.result && !tool.error;
  const checkFailed = tool.name === "run_check" && tool.result && tool.result.exit_code !== 0;
  const status = failed ? "is-error" : pending ? "is-pending" : checkFailed ? "is-check-fail" : "is-ok";
  return (
    <details className={`tool ${status}`}>
      <summary>
        <Icon name={TOOL_ICONS[tool.name] ?? "info"} size={14} />
        <span className="tool-name">{TOOL_LABELS[tool.name] ?? tool.name}</span>
        <span className="tool-summary">{toolSummary(tool)}</span>
        <span className="tool-state">{failed ? "被拒绝" : pending ? "进行中" : checkFailed ? "测试失败（基线）" : "完成"}</span>
      </summary>
      <div className="tool-body">
        {tool.error && <p className="error-line"><Icon name="alert" size={14} /><span>{tool.error}</span></p>}
        <ToolResult tool={tool} />
        <details className="raw">
          <summary>原始参数与结果</summary>
          <pre className="code-block">{JSON.stringify({ name: tool.name, arguments: tool.args, result: tool.result, error: tool.error }, null, 2)}</pre>
        </details>
      </div>
    </details>
  );
}

function ToolResult({ tool }: { tool: ToolCall }) {
  const r = tool.result;
  if (!r || r.error) return null;
  if (tool.name === "read_file" && typeof r.text === "string") {
    return (
      <>
        <p className="small muted">sha256 <code>{shortHash(r.content_sha256)}</code></p>
        <pre className="code-block code-scroll" tabIndex={0} aria-label={`读取内容 ${r.path}`}>{r.text}</pre>
      </>
    );
  }
  if (tool.name === "search_code" && Array.isArray(r.hits)) {
    return r.hits.length ? (
      <ul className="hits">
        {r.hits.map((hit: { path: string; line: number; text: string }, index: number) => (
          <li key={index}><code className="hit-loc">{hit.path}:{hit.line}</code><code className="hit-text">{hit.text}</code></li>
        ))}
      </ul>
    ) : <p className="small muted">没有匹配。</p>;
  }
  if (tool.name === "run_check" && typeof r.output === "string") {
    return (
      <>
        <p className="small"><RunnerTag runner={r.runner ?? "unknown"} /></p>
        <OutputBlock output={r.output} label="基线检查输出" />
      </>
    );
  }
  if (tool.name === "propose_patch" && r.proposal_digest) {
    return <p className="small">补丁 digest <code>{r.proposal_digest}</code> · 状态 <code>{r.status}</code></p>;
  }
  return null;
}
