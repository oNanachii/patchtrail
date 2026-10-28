import { type ReactNode, useEffect, useId, useRef, useState } from "react";
import { groupHash, shortHash } from "../lib/format.js";
import { STATES, runTone, stateLabel } from "../lib/labels.js";
import type { Run } from "../types";

const PATHS = {
  check: "M4 12.5l5 5L20 6.5",
  x: "M6 6l12 12M18 6L6 18",
  alert: "M12 4l9 16H3L12 4zM12 10v4.5M12 17.5v.01",
  info: "M12 21a9 9 0 100-18 9 9 0 000 18zM12 11v6M12 7.5v.01",
  clock: "M12 21a9 9 0 100-18 9 9 0 000 18zM12 7v5l3 2",
  lock: "M6 11h12v9H6zM8.5 11V8a3.5 3.5 0 017 0v3",
  unlock: "M6 11h12v9H6zM8.5 11V8a3.5 3.5 0 016.8-1.2",
  play: "M8 5.5v13l10.5-6.5L8 5.5z",
  stop: "M7 7h10v10H7z",
  resume: "M4 12a8 8 0 0113.7-5.6M20 4v4.5h-4.5M20 12a8 8 0 01-13.7 5.6M4 20v-4.5h4.5",
  download: "M12 4v11m0 0l-4.5-4.5M12 15l4.5-4.5M5 19.5h14",
  copy: "M9 9h10v11H9zM5 15V4h10",
  file: "M6 3h8l4 4v14H6zM14 3v4h4",
  search: "M10.5 17a6.5 6.5 0 100-13 6.5 6.5 0 000 13zM15.5 15.5L20 20",
  terminal: "M4 5h16v14H4zM7.5 9.5l3 2.5-3 2.5M12.5 15h4",
  patch: "M7 4v10M7 14a3 3 0 100 6 3 3 0 000-6zM17 20V10a4 4 0 00-4-4h-3M12 3.5L9.5 6 12 8.5",
  shield: "M12 3l7.5 3v5.5c0 4.5-3.2 8-7.5 9.5-4.3-1.5-7.5-5-7.5-9.5V6L12 3z",
  spark: "M12 3l1.8 5.4L19 10l-5.2 1.6L12 17l-1.8-5.4L5 10l5.2-1.6L12 3zM18.5 16l.7 2 2 .7-2 .7-.7 2-.7-2-2-.7 2-.7.7-2z",
  script: "M8 4h10v13a3 3 0 01-3 3H6a3 3 0 01-3-3v-1h11v1a3 3 0 003 3M8 4a2 2 0 00-2 2v10M11 8.5h4M11 12h4",
  back: "M15 5l-7 7 7 7",
  chevron: "M9 6l6 6-6 6",
  sun: "M12 16a4 4 0 100-8 4 4 0 000 8zM12 2.5v2M12 19.5v2M4.6 4.6L6 6M18 18l1.4 1.4M2.5 12h2M19.5 12h2M4.6 19.4L6 18M18 6l1.4-1.4",
  moon: "M20 14.5A8 8 0 019.5 4a8 8 0 1010.5 10.5z",
  system: "M3.5 5h17v11h-17zM8.5 20h7M12 16v4",
  gate: "M12 3l8 9-8 9-8-9 8-9z",
  flag: "M5 21V4M5 4h11l-2 4 2 4H5",
  link: "M10 14a4 4 0 005.7 0l3-3a4 4 0 00-5.7-5.7l-1 1M14 10a4 4 0 00-5.7 0l-3 3a4 4 0 005.7 5.7l1-1",
  wifi: "M3 9a13 13 0 0118 0M6 12.5a8.5 8.5 0 0112 0M9 16a4 4 0 016 0M12 19.5v.01",
} as const;

export type IconName = keyof typeof PATHS;

export function Icon({ name, size = 16, className }: { name: IconName; size?: number; className?: string }) {
  return (
    <svg className={className ? `icon ${className}` : "icon"} width={size} height={size} viewBox="0 0 24 24" aria-hidden="true"
      fill="none" stroke="currentColor" strokeWidth={1.8} strokeLinecap="round" strokeLinejoin="round" focusable="false">
      <path d={PATHS[name]} />
    </svg>
  );
}

export function StateBadge({ run, large }: { run: Pick<Run, "state" | "stop_reason">; large?: boolean }) {
  const meta = STATES[run.state];
  return (
    <span className={`badge tone-${runTone(run)}${meta?.active ? " is-active" : ""}${large ? " badge-lg" : ""}`}>
      <span className="badge-dot" aria-hidden="true" />
      {stateLabel(run)}
    </span>
  );
}

export function ModeTag({ mode, compact }: { mode: "demo" | "live"; compact?: boolean }) {
  return mode === "live" ? (
    <span className="mode-tag mode-live" title="真实 DeepSeek 模型调用">
      <Icon name="spark" size={13} />
      {compact ? "LIVE" : "DeepSeek 真实调用"}
    </span>
  ) : (
    <span className="mode-tag mode-demo" title="脚本演示：预设动作，不调用模型">
      <Icon name="script" size={13} />
      {compact ? "演示" : "脚本演示"}
    </span>
  );
}

export function CopyButton({ value, label }: { value: string; label: string }) {
  const [copied, setCopied] = useState<"idle" | "ok" | "fail">("idle");
  useEffect(() => {
    if (copied === "idle") return;
    const timer = window.setTimeout(() => setCopied("idle"), 1600);
    return () => window.clearTimeout(timer);
  }, [copied]);
  return (
    <button type="button" className="icon-button" aria-label={`复制${label}`} title={`复制${label}`}
      onClick={async () => {
        try {
          await navigator.clipboard.writeText(value);
          setCopied("ok");
        } catch {
          setCopied("fail");
        }
      }}>
      <Icon name={copied === "ok" ? "check" : "copy"} size={14} />
      <span className="sr-only" role="status">{copied === "ok" ? "已复制" : copied === "fail" ? "复制失败，请手动选择" : ""}</span>
    </button>
  );
}

/** Short form inline; the full value is selectable and copyable. */
export function Hash({ value, label, full }: { value: string | null | undefined; label: string; full?: boolean }) {
  if (!value) return <span className="mono muted">—</span>;
  return (
    <span className="hash">
      {full ? (
        <code className="hash-full" aria-label={`${label} ${value}`}>
          {groupHash(value).map((group, index) => <span key={index}>{group}</span>)}
        </code>
      ) : (
        <code title={value} aria-label={`${label} ${value}`}>{shortHash(value)}</code>
      )}
      <CopyButton value={value} label={label} />
    </span>
  );
}

/** Native modal dialog: focus containment, Escape and inert background come from the platform. */
export function Dialog({ open, onClose, title, children, footer, tone, labelledBy }: {
  open: boolean;
  onClose: () => void;
  title: ReactNode;
  children: ReactNode;
  footer?: ReactNode;
  tone?: string;
  labelledBy?: string;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  const returnFocus = useRef<HTMLElement | null>(null);
  const generated = useId();
  const headingId = labelledBy ?? `dialog-${generated}`;
  useEffect(() => {
    const dialog = ref.current;
    if (!dialog) return;
    if (open && !dialog.open) {
      const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null;
      dialog.showModal();
      // showModal() focuses the first control (the close icon); start on the marked safe action.
      dialog.querySelector<HTMLElement>("[data-autofocus]")?.focus();
      returnFocus.current = opener;
    }
    if (!open && dialog.open) dialog.close();
    if (!open && returnFocus.current) {
      // The opener can disappear (e.g. the approve button once the run moves on); fall back to the run heading.
      const target = returnFocus.current.isConnected ? returnFocus.current : document.getElementById("run-heading");
      target?.focus();
      returnFocus.current = null;
    }
  }, [open]);
  return (
    <dialog ref={ref} className={`dialog${tone ? ` tone-${tone}` : ""}`} aria-labelledby={headingId}
      onClose={onClose} onCancel={(event) => { event.preventDefault(); onClose(); }}>
      {open && (
        <div className="dialog-body">
          <header className="dialog-head">
            <h2 id={headingId}>{title}</h2>
            <button type="button" className="icon-button" onClick={onClose} aria-label="关闭">
              <Icon name="x" />
            </button>
          </header>
          <div className="dialog-content">{children}</div>
          {footer && <footer className="dialog-foot">{footer}</footer>}
        </div>
      )}
    </dialog>
  );
}

export function Callout({ tone, icon, title, children, actions }: {
  tone: string;
  icon: IconName;
  title: ReactNode;
  children?: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <div className={`callout tone-${tone}`}>
      <span className="callout-icon"><Icon name={icon} size={18} /></span>
      <div className="callout-text">
        <p className="callout-title">{title}</p>
        {children}
      </div>
      {actions && <div className="callout-actions">{actions}</div>}
    </div>
  );
}

export function Spinner({ label }: { label?: string }) {
  return <span className="spinner" role={label ? "status" : undefined} aria-label={label} />;
}

export function ErrorLine({ error }: { error: { message: string; code: string; status: number; server?: string } }) {
  return (
    <p className="error-line" role="alert">
      <Icon name="alert" size={14} />
      <span>{error.message}</span>
      <code className="error-code">{error.status ? `${error.status} · ` : ""}{error.code}</code>
    </p>
  );
}
