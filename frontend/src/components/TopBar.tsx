import { useState } from "react";
import { formatClock } from "../lib/format.js";
import type { Health, TokenStatus } from "../types";
import { Dialog, Icon } from "./ui";

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
      <StatusItem ok={known ? health.model_configured : null} warn label="DeepSeek"
        value={known ? (health.model_configured ? health.model : "未配置") : "—"} />
      <StatusItem ok={known ? health.public_demo : null} warn label="公开演示"
        value={known ? (health.public_demo ? "开放" : "关闭") : "—"} />
    </ul>
  );
}

const TOKEN_TEXT: Record<TokenStatus, string> = {
  none: "未载入令牌",
  unverified: "令牌已载入",
  accepted: "操作者",
  rejected: "令牌被拒绝",
};

export function TopBar({ health, connection, tokenStatus, onOperator, theme, onTheme }: {
  health: Health | null;
  connection: Connection;
  tokenStatus: TokenStatus;
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
        <span className="brand-sub">受控排错与补丁验证</span>
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
          <span>{TOKEN_TEXT[tokenStatus]}</span>
        </button>
        <button type="button" className="icon-button" onClick={onTheme} aria-label={`主题：${themeLabel}，点击切换`} title={`主题：${themeLabel}`}>
          <Icon name={theme === "system" ? "system" : theme === "light" ? "sun" : "moon"} />
        </button>
      </div>
    </header>
  );
}

export function OperatorDialog({ open, onClose, token, tokenStatus, health, onSave, onClear }: {
  open: boolean;
  onClose: () => void;
  token: string;
  tokenStatus: TokenStatus;
  health: Health | null;
  onSave: (value: string) => void;
  onClear: () => void;
}) {
  const [draft, setDraft] = useState("");
  const configured = health?.operator_configured;
  return (
    <Dialog open={open} onClose={() => { setDraft(""); onClose(); }} title="操作令牌"
      footer={
        <>
          {token && <button type="button" className="button" onClick={() => { setDraft(""); onClear(); }}>清除令牌</button>}
          <button type="submit" form="operator-form" className="button button-primary" disabled={!draft.trim()}>载入到本页内存</button>
        </>
      }>
      <form id="operator-form" className="stack" onSubmit={(event) => { event.preventDefault(); if (draft.trim()) { onSave(draft.trim()); setDraft(""); } }}>
        <p>live 运行的创建、查看、确认、取消、恢复和报告下载需要操作令牌。公开演示不需要。</p>
        <ul className="plain-list small">
          <li>只保存在这个页面的内存中：不写入 localStorage、sessionStorage、Cookie 或 URL，刷新或关闭页面即清除。</li>
          <li>只通过 <code>Authorization</code> 请求头发送到同源 <code>/api</code>。</li>
          <li>服务端在 live 操作时校验；公开模式下载入令牌本身不会被单独验证。</li>
        </ul>
        {configured === false && <p className="inline-warn"><Icon name="alert" size={14} />服务端没有配置操作令牌，live 功能当前不可用。</p>}
        {tokenStatus === "rejected" && <p className="inline-warn"><Icon name="alert" size={14} />当前令牌被服务端拒绝（401）。</p>}
        {token && tokenStatus !== "rejected" && <p className="small muted">本页已载入令牌{tokenStatus === "accepted" ? "，并已被服务端接受" : "（尚未被 live 操作验证）"}。</p>}
        <label className="field">
          <span>令牌</span>
          <input type="password" value={draft} onChange={(event) => setDraft(event.target.value)} autoComplete="off"
            spellCheck={false} autoCapitalize="off" name="patchtrail-operator" placeholder="粘贴操作令牌" data-autofocus />
        </label>
      </form>
    </Dialog>
  );
}
