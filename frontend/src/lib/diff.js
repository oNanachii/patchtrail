// @ts-check
/**
 * Minimal unified-diff parser for the single-file proposals the service produces.
 * @typedef {{type: "add" | "del" | "context" | "note", text: string, oldNo: number | null, newNo: number | null}} DiffLine
 * @typedef {{header: string, section: string, oldStart: number, newStart: number, lines: DiffLine[]}} Hunk
 * @typedef {{oldPath: string, newPath: string, hunks: Hunk[], added: number, removed: number}} ParsedDiff
 */

const HUNK = /^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@ ?(.*)$/;

/** @param {string} text @returns {ParsedDiff} */
export function parseUnifiedDiff(text) {
  /** @type {ParsedDiff} */
  const parsed = { oldPath: "", newPath: "", hunks: [], added: 0, removed: 0 };
  /** @type {Hunk | null} */
  let hunk = null;
  let oldNo = 0;
  let newNo = 0;
  const lines = (text ?? "").split("\n");
  if (lines.at(-1) === "") lines.pop();
  for (const line of lines) {
    if (!hunk && line.startsWith("--- ")) {
      parsed.oldPath = line.slice(4).replace(/^a\//, "");
      continue;
    }
    if (!hunk && line.startsWith("+++ ")) {
      parsed.newPath = line.slice(4).replace(/^b\//, "");
      continue;
    }
    const match = HUNK.exec(line);
    if (match) {
      oldNo = Number(match[1]);
      newNo = Number(match[3]);
      hunk = { header: line, section: match[5] ?? "", oldStart: oldNo, newStart: newNo, lines: [] };
      parsed.hunks.push(hunk);
      continue;
    }
    if (!hunk) continue;
    if (line.startsWith("\\")) {
      hunk.lines.push({ type: "note", text: line.slice(1).trim(), oldNo: null, newNo: null });
    } else if (line.startsWith("+")) {
      hunk.lines.push({ type: "add", text: line.slice(1), oldNo: null, newNo: newNo++ });
      parsed.added++;
    } else if (line.startsWith("-")) {
      hunk.lines.push({ type: "del", text: line.slice(1), oldNo: oldNo++, newNo: null });
      parsed.removed++;
    } else {
      hunk.lines.push({ type: "context", text: line.slice(1), oldNo: oldNo++, newNo: newNo++ });
    }
  }
  return parsed;
}
