import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { NewRun, type CreateFailure } from "./components/NewRun";
import type { ApprovalSnapshot } from "./components/PatchReview";
import { RunDetail, type RunActions } from "./components/RunDetail";
import { RunList } from "./components/RunList";
import { OperatorDialog, TopBar, type Connection } from "./components/TopBar";
import { ErrorLine, Icon, Spinner } from "./components/ui";
import { usePoll, useHashRun, useMediaQuery, useNow, useTheme } from "./hooks";
import { createApi } from "./lib/api.js";
import { createAuthEpoch, obsoleteAuthError } from "./lib/authEpoch.js";
import { pendingStore, submitCreate } from "./lib/createGuard.js";
import { formatClock } from "./lib/format.js";
import { describeError, stateLabel, TERMINAL, WORKING } from "./lib/labels.js";
import type { Health, Run, RunEvent, Task, TokenStatus } from "./types";

type Described = ReturnType<typeof describeError> & { network?: boolean };

const isAbort = (error: unknown) => error instanceof Error && error.name === "AbortError";
const describe = (error: any): Described => ({ ...describeError(error), network: Boolean(error?.network) });

function newKey() {
  if (typeof crypto.randomUUID === "function") return crypto.randomUUID();
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}

function sessionStorageOrNull() {
  try {
    return window.sessionStorage;
  } catch {
    return null;
  }
}

export function App() {
  const [theme, cycleTheme] = useTheme();
  const narrow = useMediaQuery("(max-width: 959px)");
  const now = useNow(5000);

  // Operator token: React state only. Never written to storage, URL or cookies.
  const [token, setToken] = useState("");
  const tokenRef = useRef("");
  const authEpoch = useRef(createAuthEpoch());
  const onUnauthorized = useRef<(error: Described, path: string) => void>(() => {});
  const [tokenStatus, setTokenStatus] = useState<TokenStatus>("none");
  const [operatorOpen, setOperatorOpen] = useState(false);

  const [connection, setConnection] = useState<Connection>({ online: true, lastOk: null, checking: true });
  const request = useMemo(() => {
    const base = createApi({ getToken: () => tokenRef.current });
    return async (path: string, options?: Parameters<typeof base>[1]) => {
      const epoch = authEpoch.current.capture();
      try {
        const data = await authEpoch.current.guard(epoch, () => base(path, options));
        if (options?.signal?.aborted) throw obsoleteAuthError();
        setConnection({ online: true, lastOk: Date.now(), checking: false });
        return data;
      } catch (error: any) {
        if (!authEpoch.current.isCurrent(epoch) || options?.signal?.aborted) throw obsoleteAuthError();
        if (error?.network) setConnection((c) => ({ ...c, online: false, checking: false }));
        else if (!isAbort(error)) setConnection({ online: true, lastOk: Date.now(), checking: false });
        if (error?.status === 401 || error?.code === "operator_not_configured") onUnauthorized.current(describe(error), path);
        throw error;
      }
    };
  }, []);

  const [health, setHealth] = useState<Health | null>(null);
  const refreshHealth = usePoll(async (signal) => {
    try {
      setHealth(await request("/api/health", { signal }));
    } catch {
      // Connection state already reflects network failures.
    }
  }, 20000, [request]);

  const [tasks, setTasks] = useState<Task[] | null>(null);
  const [tasksError, setTasksError] = useState<string | null>(null);
  usePoll(async (signal) => {
    try {
      const data = await request("/api/tasks", { signal });
      setTasks(data.tasks);
      setTasksError(null);
    } catch (error) {
      if (!isAbort(error)) setTasksError(describe(error).message);
    }
  }, tasks ? null : 5000, [request, Boolean(tasks)]);

  const [runs, setRuns] = useState<Run[] | null>(null);
  const [runsError, setRunsError] = useState<Described | null>(null);
  const anyWorking = runs?.some((run) => WORKING.has(run.state)) ?? false;
  const refreshRuns = usePoll(async (signal) => {
    const epoch = authEpoch.current.capture();
    try {
      const data = await request("/api/runs", { signal });
      authEpoch.current.assertCurrent(epoch);
      setRuns(data.runs);
      setRunsError(null);
      if (tokenRef.current && data.runs.some((run: Run) => run.mode === "live")) setTokenStatus("accepted");
    } catch (error: any) {
      if (isAbort(error) || !authEpoch.current.isCurrent(epoch)) return;
      setRunsError(describe(error));
      if (error?.status === 401) setRuns(null);
    }
  }, anyWorking ? 2500 : 6000, [request, token]);

  // Selected run and its incrementally fetched events.
  const [selectedId, select] = useHashRun();
  const [focusKey, setFocusKey] = useState(0);
  const [detail, setDetail] = useState<{ id: string | null; run: Run | null; events: RunEvent[]; error: Described | null }>(
    { id: null, run: null, events: [], error: null });
  const cursor = useRef<{ id: string | null; after: number }>({ id: null, after: 0 });
  if (detail.id !== selectedId) {
    cursor.current = { id: selectedId, after: 0 };
    setDetail({ id: selectedId, run: null, events: [], error: null });
  }
  const detailDelay = !selectedId ? null
    : detail.error && [401, 404].includes(detail.error.status) ? null
    : !detail.run ? 4000
    : WORKING.has(detail.run.state) ? 1500
    : TERMINAL.has(detail.run.state) ? null : 6000;
  const refreshDetail = usePoll(async (signal) => {
    const id = selectedId;
    const epoch = authEpoch.current.capture();
    if (!id) return;
    try {
      // Run first, then events: a terminal run's events are then complete when polling stops.
      const run: Run = await request(`/api/runs/${id}`, { signal });
      const fresh: RunEvent[] = [];
      let after = cursor.current.id === id ? cursor.current.after : 0;
      for (;;) {
        const { events } = await request(`/api/runs/${id}/events?after=${after}`, { signal });
        fresh.push(...events);
        if (events.length) after = events[events.length - 1].id;
        if (events.length < 1000) break;
      }
      authEpoch.current.assertCurrent(epoch);
      if (signal.aborted || cursor.current.id !== id) return;
      cursor.current.after = after;
      if (run.mode === "live" && tokenRef.current) setTokenStatus("accepted");
      setDetail((prev) => prev.id !== id ? prev : {
        id,
        run,
        events: fresh.length ? [...prev.events, ...fresh.filter((e) => !prev.events.some((p) => p.id === e.id))] : prev.events,
        error: null,
      });
    } catch (error) {
      if (isAbort(error) || !authEpoch.current.isCurrent(epoch)) return;
      const described = describe(error);
      const denied = described.status === 401 || described.code === "operator_not_configured";
      if (denied && cursor.current.id === id) cursor.current.after = 0;
      setDetail((prev) => prev.id !== id ? prev : denied
        ? { id, run: null, events: [], error: described }
        : { ...prev, error: described });
    }
  }, detailDelay, [request, selectedId, token]);

  // Announce state changes of the selected run to assistive technology.
  const [announcement, setAnnouncement] = useState("");
  const lastState = useRef<string | null>(null);
  useEffect(() => {
    const run = detail.run;
    const key = run ? `${run.id}:${run.state}` : null;
    if (run && lastState.current && lastState.current !== key && lastState.current.startsWith(run.id)) {
      setAnnouncement(`运行状态：${stateLabel(run)}`);
    }
    lastState.current = key;
  }, [detail.run]);

  const applyRun = useCallback((run: Run) => {
    setDetail((prev) => prev.id === run.id ? { ...prev, run } : prev);
  }, []);

  const actions: RunActions = useMemo(() => ({
    approve: async (snapshot: ApprovalSnapshot) => {
      try {
        applyRun(await request(`/api/runs/${snapshot.runId}/approve`, {
          method: "POST", body: { proposal_digest: snapshot.digest, expected_version: snapshot.version },
        }));
      } catch (error) {
        throw describe(error);
      } finally {
        refreshDetail();
        refreshRuns();
      }
    },
    cancel: async (run: Run) => {
      try {
        applyRun(await request(`/api/runs/${run.id}/cancel`, { method: "POST" }));
      } catch (error) {
        throw describe(error);
      } finally {
        refreshDetail();
        refreshRuns();
      }
    },
    resume: async (run: Run, version: number) => {
      try {
        applyRun(await request(`/api/runs/${run.id}/resume`, { method: "POST", body: { expected_version: version } }));
      } catch (error) {
        throw describe(error);
      } finally {
        refreshDetail();
        refreshRuns();
      }
    },
    download: async (run: Run) => {
      let report;
      try {
        report = await request(`/api/runs/${run.id}/report`);
      } catch (error) {
        throw describe(error);
      }
      const url = URL.createObjectURL(new Blob([JSON.stringify(report, null, 2)], { type: "application/json" }));
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = `patchtrail-report-${run.id}.json`;
      document.body.append(anchor);
      anchor.click();
      anchor.remove();
      window.setTimeout(() => URL.revokeObjectURL(url), 1000);
    },
  }), [request, applyRun, refreshDetail, refreshRuns]);

  // Idempotent creation; see lib/createGuard.js.
  const store = useMemo(() => pendingStore(sessionStorageOrNull()), []);
  const [pending, setPending] = useState(store.get());
  const [creating, setCreating] = useState(false);
  const [createFailure, setCreateFailure] = useState<CreateFailure | null>(null);
  const start = async (selection: { task_id: string; mode: "demo" | "live" }) => {
    if (creating) return;
    setCreating(true);
    setCreateFailure(null);
    try {
      const { run } = await submitCreate({ store, request, selection, newKey });
      if (run.mode === "live" && tokenRef.current) setTokenStatus("accepted");
      setFocusKey((k) => k + 1);
      select(run.id);
      refreshRuns();
    } catch (error: any) {
      if (isAbort(error)) return;
      setCreateFailure({ ...describe(error), outcome: error?.createOutcome ?? "unknown" });
      if (error?.code === "sandbox_unavailable" || error?.code === "model_not_configured") refreshHealth();
    } finally {
      setPending(store.get());
      setCreating(false);
    }
  };

  // Clear every cached run on a credential boundary. This also covers private-demo
  // deployments, where demo records require the same operator credential as live.
  const clearRunCaches = (error: Described | null = null) => {
    cursor.current = { id: selectedId, after: 0 };
    setDetail({ id: selectedId, run: null, events: [], error });
    setRuns(null);
    setRunsError(null);
  };
  const replaceToken = (value: string) => {
    authEpoch.current.advance();
    tokenRef.current = value.trim();
    setToken(tokenRef.current);
    setTokenStatus(tokenRef.current ? "unverified" : "none");
    clearRunCaches();
    setCreateFailure(null);
    setOperatorOpen(false);
    refreshHealth();
    refreshRuns();
    refreshDetail();
  };
  onUnauthorized.current = (error, path) => {
    if (tokenRef.current) {
      authEpoch.current.advance();
      tokenRef.current = "";
      setToken("");
      setTokenStatus("rejected");
      clearRunCaches(error);
    } else if (health?.public_demo === false) {
      clearRunCaches(error);
    } else if (selectedId && path.startsWith(`/api/runs/${selectedId}`)) {
      // An anonymous live-detail denial must not erase a concurrent public list.
      cursor.current = { id: selectedId, after: 0 };
      setDetail({ id: selectedId, run: null, events: [], error });
    }
  };

  const chooseRun = (id: string) => {
    if (narrow) setFocusKey((k) => k + 1);
    select(id);
  };

  const mainRef = useRef<HTMLElement>(null);
  const view = narrow && selectedId ? "detail" : "list";

  return (
    <div className="app" data-view={view}>
      <a className="skip-link" href="#run-heading" onClick={(event) => { event.preventDefault(); mainRef.current?.focus(); }}>跳到运行详情</a>
      <TopBar health={health} connection={connection} tokenStatus={tokenStatus} onOperator={() => setOperatorOpen(true)}
        theme={theme} onTheme={cycleTheme} />
      {health?.design_harness && (
        <div className="banner banner-harness" role="note">
          <Icon name="alert" size={16} />
          <p><strong>设计测试环境，不是证据。</strong>假 runner：未启动容器、未执行代码；live 未配置模型密钥。<span className="mono-note">{health.design_harness}</span></p>
        </div>
      )}
      {!connection.online && (
        <div className="banner banner-offline" role="alert">
          <Icon name="wifi" size={16} />
          <p><strong>无法连接 PatchTrail 服务。</strong>页面显示上次同步的数据（{formatClock(connection.lastOk)}），正在自动重试。</p>
          <button type="button" className="button" onClick={() => { refreshHealth(); refreshRuns(); refreshDetail(); }}>立即重试</button>
        </div>
      )}
      <div className="shell">
        <aside className="sidebar" aria-label="新建与最近运行">
          <NewRun tasks={tasks} tasksError={tasksError} health={health} online={connection.online} token={token}
            tokenStatus={tokenStatus} pending={pending} busy={creating} failure={createFailure} onStart={start}
            onOperator={() => setOperatorOpen(true)} />
          <RunList runs={runs && detail.run ? runs.map((run) => run.id === detail.run!.id && detail.run!.version > run.version ? detail.run! : run) : runs} error={runsError} tasks={tasks} selectedId={selectedId} onSelect={chooseRun}
            tokenStatus={tokenStatus} publicDemo={health?.public_demo} now={now} onOperator={() => setOperatorOpen(true)} />
        </aside>
        <main className="main" ref={mainRef} tabIndex={-1} aria-label="运行详情">
          {narrow && selectedId && (
            <button type="button" className="back-button" onClick={() => select(null)}>
              <Icon name="back" size={16} />全部运行
            </button>
          )}
          {!selectedId ? <Overview /> : detail.run ? (
            <RunDetail run={detail.run} events={detail.events} error={detail.error} tasks={tasks} health={health}
              now={now} actions={actions} focusKey={focusKey} />
          ) : detail.error ? (
            <DetailError error={detail.error} id={selectedId} onOperator={() => setOperatorOpen(true)} onRetry={refreshDetail} />
          ) : (
            <div className="detail-loading"><Spinner label="正在读取运行" /> 正在读取运行 <code>{selectedId.slice(0, 8)}</code>…</div>
          )}
        </main>
      </div>
      <footer className="footer">
        <span>PatchTrail {health ? `v${health.version}` : ""}</span>
        <span>脚本运行时检查、真实模型效果与人工验收分别记录；不代表通用修复准确率。</span>
        <span>API 契约 <code>/api/openapi.json</code></span>
      </footer>
      <div className="sr-only" role="status" aria-live="polite">{announcement}</div>
      <OperatorDialog open={operatorOpen} onClose={() => setOperatorOpen(false)} token={token} tokenStatus={tokenStatus} health={health}
        onSave={replaceToken}
        onClear={() => replaceToken("")} />
    </div>
  );
}

function DetailError({ error, id, onOperator, onRetry }: { error: Described; id: string; onOperator: () => void; onRetry: () => void }) {
  if (error.status === 401) {
    return (
      <div className="detail-state">
        <Icon name="lock" size={28} />
        <h2>这次运行需要操作令牌</h2>
        <p>live 运行的详情、事件和报告只对操作者开放。令牌只保存在本页内存中。</p>
        <button type="button" className="button button-primary" onClick={onOperator}>载入令牌</button>
      </div>
    );
  }
  if (error.status === 404) {
    return (
      <div className="detail-state">
        <Icon name="search" size={28} />
        <h2>找不到运行 <code>{id.slice(0, 12)}</code></h2>
        <p>它可能属于另一个数据目录，或者链接有误。</p>
      </div>
    );
  }
  return (
    <div className="detail-state">
      <ErrorLine error={error} />
      <button type="button" className="button" onClick={onRetry}>重试</button>
    </div>
  );
}

function Overview() {
  return (
    <div className="overview">
      <p className="eyebrow">受控任务 · 可核对的证据链</p>
      <h1>从一个失败测试，走到可核对的修复。</h1>
      <p className="lede">选择一个预置任务。Agent 在受限副本里读代码、复现失败、提出单文件补丁后停下；你按准确的 digest 与版本确认；验证器在新工作区的禁网容器里运行不可修改的测试，由测试结果决定成功与否。</p>
      <ol className="flow">
        <li><span className="flow-n">1</span><div><strong>调查</strong><p>有界读文件、字面搜索、固定检查。文件和工具输出只当作数据。</p></div></li>
        <li><span className="flow-n">2</span><div><strong>复现</strong><p>在容器中运行原始测试，确认基线确实失败。</p></div></li>
        <li className="is-gate"><span className="flow-n"><Icon name="gate" size={14} /></span><div><strong>人工确认</strong><p>补丁停在这里。确认绑定 proposal_digest 与 expected_version，而不是只按运行 ID。</p></div></li>
        <li><span className="flow-n">4</span><div><strong>独立验证</strong><p>从不可变任务重建工作区，应用这份补丁，运行不可修改的测试。</p></div></li>
      </ol>
      <div className="boundaries">
        <h2>证据边界</h2>
        <ul>
          <li><strong>脚本演示</strong>不调用模型，补丁是预置修复：它证明运行时与验证器可用，不证明模型能力。</li>
          <li><strong>DeepSeek 真实调用</strong>需要操作令牌；模型文本和补丁说明不会被当作结果。</li>
          <li>三个任务是根据已观察问题独立编写的公开回归案例，不是盲评或 benchmark 成绩。</li>
          <li>容器不可用时服务返回 503，不会转到宿主机执行。</li>
        </ul>
      </div>
    </div>
  );
}
