import { useEffect, useRef, useState } from "react";
import { isSecureCredentialOrigin } from "../lib/sessionState.js";
import { formatClock } from "../lib/format.js";
import type { Health, TokenStatus, SessionInfo } from "../types";
import { Dialog, Icon, Spinner } from "./ui";

export type Connection = { online: boolean; lastOk: number | null; checking: boolean };

function StatusItem({ ok, label, value, warn }: { ok: boolean | null; label: string; value: string; warn?: boolean }) {
  const tone = ok == null ? "unknown" : ok ? "ok" : warn ? "warn" : "bad";
  return (
    <li className={`status-item status-${tone}`}>
      <span className="status-dot" aria-hidden="true" />
      <span className="status-label">{label}</span>
      <span className="status-value">{value}</span>
    </li>
  );
}

export function ServiceStatus({ health, connection }: { health: Health | null; connection: Connection }) {
  const known = health && connection.online;
  return (
    <ul className="status-list" aria-label="服务状态">
      <StatusItem ok={connection.online ? (health ? true : null) : false} label="API"
        value={connection.online ? (health ? `v${health.version}` : "检查中") : "失联"} />
      <StatusItem ok={known ? health.container_available : null} label="容器"
        value={known ? (health.container_available ? "可用" : "不可用") : "—"} />
      <StatusItem ok={known ? Boolean(health.byok_available || health.model_configured) : null} warn label="DeepSeek"
        value={known ? (health.byok_available ? "自带 Key" : health.model_configured ? health.model : "未配置") : "—"} />
      <StatusItem ok={known ? health.public_demo : null} warn label="公开演示"
        value={known ? (health.public_demo ? "开放" : "关闭") : "—"} />
    </ul>
  );
}

const TOKEN_TEXT: Record<TokenStatus, string> = {
  none: "连接访问",
  unverified: "权限待确认",
  accepted: "已连接",
  rejected: "连接已失效",
};

export function TopBar({ health, connection, tokenStatus, onOperator, theme, onTheme, session }: {
  health: Health | null;
  connection: Connection;
  tokenStatus: TokenStatus;
  session?: SessionInfo | null;
  onOperator: () => void;
  theme: string;
  onTheme: () => void;
}) {
  const themeLabel = theme === "system" ? "跟随系统" : theme === "light" ? "浅色" : "深色";
  return (
    <header className="topbar">
      <a className="brand" href="#" aria-label="PatchTrail 首页">
        <svg className="brand-mark" viewBox="0 0 32 32" aria-hidden="true">
          <path d="M6 24 L12.5 15 L19 18.5 L26 8" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" strokeDasharray="0.1 4.4" />
          <circle cx="6" cy="24" r="2.6" fill="currentColor" />
          <circle cx="26" cy="8" r="3.4" className="brand-end" />
        </svg>
        <span className="brand-name">PatchTrail</span>
        <span className="brand-sub">Python 测试失败修复</span>
      </a>
      <details className="status-menu">
        <summary aria-label="服务状态">
          <span className={`status-summary-dot ${connection.online ? (health?.container_available ? "ok" : "warn") : "bad"}`} aria-hidden="true" />
          <span>服务状态</span>
        </summary>
        <div className="status-pop">
          <ServiceStatus health={health} connection={connection} />
          {health?.runtime_code_hash && <p className="status-meta mono">runtime {health.runtime_code_hash.slice(0, 12)}</p>}
          <p className="status-meta">最后成功同步 {formatClock(connection.lastOk)}</p>
        </div>
      </details>
      <div className="status-inline">
        <ServiceStatus health={health} connection={connection} />
      </div>
      <div className="topbar-actions">
        <button type="button" className={`operator-button token-${tokenStatus}`} onClick={onOperator} aria-haspopup="dialog">
          <Icon name={tokenStatus === "none" || tokenStatus === "rejected" ? "lock" : "unlock"} size={15} />
          <span>{tokenStatus === "accepted" ? session?.role === "member" ? "已连接 · 自带 Key" : "操作者" : TOKEN_TEXT[tokenStatus]}</span>
        </button>
        <button type="button" className="icon-button" onClick={onTheme} aria-label={`主题：${themeLabel}，点击切换`} title={`主题：${themeLabel}`}>
          <Icon name={theme === "system" ? "system" : theme === "light" ? "sun" : "moon"} />
        </button>
      </div>
    </header>
  );
}

export function OperatorDialog({ open, onClose, token, tokenStatus, health, session, onConnect, onSave, onClear }: {
  open: boolean; onClose: () => void; token: string; tokenStatus: TokenStatus; health: Health | null;
  session: SessionInfo | null; onConnect: (invitation: string, apiKey: string) => Promise<void>;
  onSave: (value: string) => void; onClear: () => void;
}) {
  const [draft, setDraft] = useState("");
  const [invitation, setInvitation] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const attempt = useRef(0);
  const credentialAllowed = typeof window !== "undefined" && isSecureCredentialOrigin(window.location.origin);
  useEffect(() => {
    if (!open) { attempt.current++; setDraft(""); setInvitation(""); setApiKey(""); setBusy(false); setError(""); }
  }, [open]);
  const close = () => { attempt.current++; setDraft(""); setInvitation(""); setApiKey(""); setError(""); setBusy(false); onClose(); };
  const connect = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!invitation.trim() || !apiKey.trim() || busy || !credentialAllowed) return;
    const ticket = ++attempt.current;
    const code = invitation.trim(), key = apiKey.trim();
    setApiKey(""); setBusy(true); setError("");
    try { await onConnect(code,key); }
    catch (caught: any) {
      if (ticket !== attempt.current || caught?.name === "AbortError") return;
      setError(caught?.message || "连接失败，请重试。尚未调用模型。");
    } finally { if (ticket === attempt.current) setBusy(false); }
  };
  return (
    <Dialog open={open} onClose={close} title={session ? "当前连接" : "连接你的 DeepSeek Key"}
      footer={<><button type="button" className="button" onClick={close}>关闭</button>{token && <button type="button" className="button button-danger-quiet" onClick={() => { setApiKey(""); setInvitation(""); onClear(); }}>退出并清除本页凭据</button>}</>}>
      {session && <div className="session-summary" role="status">
        <p><strong>{session.role === "member" ? "成员访问 · 自带 Key" : "原操作者访问"}</strong></p>
        <p className="small muted">模型 {session.model}{session.expires_at ? ` · 会话有效至 ${new Date(session.expires_at * 1000).toLocaleTimeString()}` : ""}。项目与运行保留在你的独立空间。</p>
      </div>}
      {!credentialAllowed && <p className="inline-warn" role="alert">当前连接不是HTTPS，凭据输入与发送已禁用。请通过本站HTTPS地址访问后再连接。</p>}
      <form className="stack" onSubmit={connect}>
        <p>用管理员单独发给你的邀请码连接。登录只建立短期会话，<strong>不会调用模型或产生模型费用。</strong></p>
        <label className="field"><span>邀请码</span><input type="password" value={invitation} onChange={(event) => setInvitation(event.target.value)} disabled={busy || !credentialAllowed} autoComplete="off" spellCheck={false} autoCapitalize="off" placeholder="粘贴你的独立邀请码" data-autofocus /></label>
        <label className="field"><span>DeepSeek API Key</span><input type="password" value={apiKey} onChange={(event) => setApiKey(event.target.value)} disabled={busy || !credentialAllowed} autoComplete="off" spellCheck={false} autoCapitalize="off" placeholder="你的 DeepSeek Key" /></label>
        <p className="small muted">Key发送到本站后仅在服务器内存短期保留，提交即清空输入框，不写入浏览器存储或运行记录。只调用DeepSeek官方接口，模型由服务器配置。</p>
        <button type="submit" className="button button-primary" disabled={busy || !credentialAllowed || !invitation.trim() || !apiKey.trim()}>{busy ? <Spinner/> : <Icon name="lock" size={15}/>} {busy ? "正在连接，不调用模型…" : "连接（不调用模型）"}</button>
        {error && <p className="error-line" role="alert">{error}</p>}
        {tokenStatus === "rejected" && <p className="inline-warn">会话已失效或邀请码被撤销，请重新连接。</p>}
      </form>
      <p className="small muted">平台不收费；真实模型费用由你的DeepSeek账户承担。代码只在你另行确认真实调用后发送给模型。退出或过期后停止新请求，已发出的请求仍可能完成并计费。</p>
      {session?.role !== "member" && <details className="model-help small">
        <summary>已有操作者入口</summary>
        <form className="stack" onSubmit={(event) => { event.preventDefault(); if (draft.trim() && credentialAllowed) { onSave(draft.trim()); setDraft(""); } }}>
          <p>仅供原部署操作者使用独立操作令牌；成员使用上方邀请码与自己的Key。</p>
          <label className="field"><span>原操作令牌</span><input type="password" value={draft} disabled={!credentialAllowed} onChange={(event) => setDraft(event.target.value)} autoComplete="off" spellCheck={false} autoCapitalize="off" placeholder="粘贴原操作令牌" /></label>
          <button className="button" type="submit" disabled={!credentialAllowed || !draft.trim() || busy || health?.operator_configured === false}>验证操作者身份</button>
        </form>
      </details>}
      <p className="small muted">会话凭据只在本页内存，刷新会断开。本页只保留按身份隔离的待核对请求编号，以便响应丢失后核对原运行；不保存Key、会话令牌或源码。</p>
    </Dialog>
  );
}
