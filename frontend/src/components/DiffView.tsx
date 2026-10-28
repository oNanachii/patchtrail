import { useMemo, useState } from "react";
import { parseUnifiedDiff } from "../lib/diff.js";

export function DiffView({ diff, label }: { diff: string; label: string }) {
  const parsed = useMemo(() => parseUnifiedDiff(diff), [diff]);
  const [wrap, setWrap] = useState(() => window.matchMedia("(max-width: 600px)").matches);
  if (!parsed.hunks.length) {
    return <pre className="code-block" tabIndex={0} aria-label={label}>{diff || "（空 diff）"}</pre>;
  }
  return (
    <div className="diff">
      <div className="diff-bar">
        <code className="diff-path">{parsed.newPath || parsed.oldPath}</code>
        <span className="diff-stat">
          <span className="diff-add-count">+{parsed.added}</span>
          <span className="diff-del-count">−{parsed.removed}</span>
        </span>
        <label className="toggle">
          <input type="checkbox" checked={wrap} onChange={(event) => setWrap(event.target.checked)} />
          <span>自动换行</span>
        </label>
      </div>
      <div className={`diff-scroll${wrap ? " is-wrapped" : ""}`} tabIndex={0} role="region" aria-label={label}>
        <table className="diff-table">
          <colgroup><col className="diff-col-no" /><col className="diff-col-no" /><col className="diff-col-sign" /><col /></colgroup>
          <tbody>
            {parsed.hunks.map((hunk, h) => (
              <HunkRows key={h} hunk={hunk} />
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

const SIGN = { add: "+", del: "-", context: " ", note: "\\" } as const;
const SR = { add: "新增：", del: "删除：", context: "", note: "" } as const;

function HunkRows({ hunk }: { hunk: ReturnType<typeof parseUnifiedDiff>["hunks"][number] }) {
  return (
    <>
      <tr className="diff-hunk">
        <td colSpan={4}><code>{hunk.header}</code></td>
      </tr>
      {hunk.lines.map((line, index) => (
        <tr key={index} className={`diff-line diff-${line.type}`}>
          <td className="diff-no" aria-hidden="true">{line.oldNo ?? ""}</td>
          <td className="diff-no" aria-hidden="true">{line.newNo ?? ""}</td>
          <td className="diff-sign" aria-hidden="true">{SIGN[line.type]}</td>
          <td className="diff-code"><span className="sr-only">{SR[line.type]}</span>{line.text || " "}</td>
        </tr>
      ))}
    </>
  );
}
