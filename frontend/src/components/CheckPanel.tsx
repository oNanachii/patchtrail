import { useMemo, useState } from "react";
import { compareTests, isCheckResult, parseUnittest, verdictCriteria, verificationEvidence } from "../lib/checks.js";
import { formatDuration } from "../lib/format.js";
import { runStopReason, TERMINAL } from "../lib/labels.js";
import { findBaseline } from "../lib/trail.js";
import type { CheckResultData, Run, RunEvent } from "../types";
import { Icon } from "./ui";

const CONTAINER_RUNNERS = new Set(["docker", "podman"]);
const STATUS_TEXT: Record<string, string> = { ok: "通过", fail: "失败", error: "错误", skipped: "跳过" };

export function RunnerTag({ runner }: { runner: string }) {
  const container = CONTAINER_RUNNERS.has(runner);
  return (
    <span className={`runner-tag${container ? "" : " is-fake"}`} title={container ? "OCI 容器执行" : "不是容器执行器"}>
      <Icon name={container ? "shield" : "alert"} size={13} />
      <code>{runner}</code>
      {!container && <span>非容器执行器 · 结果不是证据</span>}
    </span>
  );
}

export function OutputBlock({ output, label, initiallyOpen = false }: { output: string; label: string; initiallyOpen?: boolean }) {
  const [expanded, setExpanded] = useState(initiallyOpen);
  const lines = output.replace(/\n$/, "").split("\n");
  return (
    <div className="output">
      <div className={`output-scroll${expanded ? " is-expanded" : ""}`} tabIndex={0} role="region" aria-label={label}>
        <pre>
          {lines.map((line, index) => (
            <span key={index} className={lineClass(line)}>{line}{"\n"}</span>
          ))}
        </pre>
      </div>
      {lines.length > 14 && (
        <button type="button" className="link-button output-toggle" onClick={() => setExpanded((v) => !v)} aria-expanded={expanded}>
          {expanded ? "收起输出" : `展开全部 ${lines.length} 行`}
        </button>
      )}
    </div>
  );
}

function lineClass(line: string) {
  if (/ \.\.\. ok$/.test(line) || line === "OK") return "out-ok";
  if (/ \.\.\. (FAIL|ERROR)$/.test(line) || /^(FAIL|ERROR):/.test(line) || /^FAILED/.test(line) || /Error\b/.test(line)) return "out-bad";
  if (/^\[design harness\]/.test(line)) return "out-warn";
  if (/^(-{20,}|={20,})$/.test(line)) return "out-rule";
  return "";
}

export function CheckPanel({ run, events }: { run: Run; events: RunEvent[] }) {
  const baseline = useMemo(() => findBaseline(events) as CheckResultData | null, [events]);
  const result = run.result;
  const verified = isCheckResult(result) ? (result as CheckResultData) : null;
  const failure = result && !verified ? (result as { error?: string }) : null;
  const before = useMemo(() => (baseline ? parseUnittest(baseline.output) : null), [baseline]);
  const after = useMemo(() => (verified ? parseUnittest(verified.output) : null), [verified]);
  const rows = compareTests(before, after);

  return (
    <section className="panel check-panel" aria-labelledby="check-title" id="checks">
      <header className="section-head">
        <h3 id="check-title"><Icon name="terminal" />检查结果</h3>
      </header>

      {verified ? <Verdict result={verified} /> : failure ? <FailureCard run={run} error={failure.error} /> : (
        <p className="muted small not-yet">
          {TERMINAL.has(run.state) ? "这次运行没有产生验证输出。" : run.state === "verifying" ? "正在新工作区中运行不可修改的测试…" : "确认补丁后，验证器的实际输出会出现在这里。"}
        </p>
      )}

      {rows.length > 0 && (
        <div className="test-matrix-wrap">
          <table className="test-matrix">
            <caption>逐个用例：基线（修复前）与验证（修复后）</caption>
            <thead>
              <tr><th scope="col">用例</th><th scope="col">修复前</th><th scope="col">修复后</th></tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.name}>
                  <th scope="row"><code>{row.name}</code></th>
                  <td><TestStatus status={row.before} /></td>
                  <td><TestStatus status={row.after} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {verified && (
        <details className="output-details" open={!verified.passed}>
          <summary>验证输出 · {formatDuration(verified.duration_ms)} · <code>{verified.runner}</code></summary>
          <OutputBlock output={verified.output || "（无输出）"} label="验证器实际输出" />
        </details>
      )}
      {baseline && (
        <details className="output-details">
          <summary>基线输出（修复前） · exit {baseline.exit_code ?? "—"} · {formatDuration(baseline.duration_ms)} · <code>{baseline.runner}</code></summary>
          <OutputBlock output={baseline.output || "（无输出）"} label="基线检查实际输出" />
        </details>
      )}
    </section>
  );
}

function TestStatus({ status }: { status: string | null }) {
  if (!status) return <span className="muted">—</span>;
  const tone = status === "ok" ? "success" : status === "skipped" ? "muted" : "danger";
  return <span className={`test-status tone-${tone}`}><Icon name={status === "ok" ? "check" : status === "skipped" ? "info" : "x"} size={12} />{STATUS_TEXT[status] ?? status}</span>;
}

function Verdict({ result }: { result: CheckResultData }) {
  const criteria = verdictCriteria(result);
  const evidence = verificationEvidence(result);
  const unconfirmed = !evidence.complete;
  const passed = !unconfirmed && (result.passed ?? criteria.every((c) => c.ok));
  const title = unconfirmed ? "检查未确认完成，不能判断补丁" : passed ? "验证通过" : "验证未通过";
  return (
    <div className={`verdict ${unconfirmed ? "tone-warning" : passed ? "tone-success" : "tone-danger"}`}>
      <div className="verdict-head">
        <span className="verdict-stamp"><Icon name={unconfirmed ? "alert" : passed ? "check" : "x"} size={20} /></span>
        <div>
          <p className="verdict-title">{title}</p>
          <p className="verdict-sub">{unconfirmed ? "检查记录未确认完整" : "由验证器判定"} · {result.tests_run ?? "—"} 个用例 · exit {result.exit_code ?? "—"} · {formatDuration(result.duration_ms)}</p>
        </div>
        <RunnerTag runner={result.runner} />
      </div>
      {evidence.reason && <p className="verdict-detail">{evidence.reason}</p>}
      <ul className="criteria" aria-label="判定条件">
        {criteria.map((c) => (
          <li key={c.key} className={c.ok ? "is-ok" : "is-bad"}>
            <Icon name={c.ok ? "check" : "x"} size={13} />
            <span>{c.label}</span>
            <span className="criteria-detail">{c.detail}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

function FailureCard({ run, error }: { run: Run; error?: string }) {
  const reason = runStopReason(run);
  const warning = reason?.notExecuted || reason?.unconfirmed;
  return (
    <div className={`verdict ${warning ? "tone-warning" : "tone-danger"}`}>
      <div className="verdict-head">
        <span className="verdict-stamp"><Icon name={warning ? "alert" : "x"} size={20} /></span>
        <div>
          <p className="verdict-title">{reason?.title ?? "运行失败"}</p>
          <p className="verdict-sub">{reason?.unconfirmed ? "检查结果未确认完整，不能判断补丁" : reason?.notExecuted ? "没有产生验证结果：补丁未被执行" : "没有通过验证的补丁"}</p>
        </div>
      </div>
      {reason && <p className="verdict-detail">{reason.detail}</p>}
      {error && <p className="server-message"><span>服务端记录</span><code>{error}</code></p>}
      {run.stop_reason && <p className="small muted">stop_reason <code>{run.stop_reason}</code></p>}
    </div>
  );
}
