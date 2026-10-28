// @ts-check

/** Server timestamps carry microseconds; trim to milliseconds for every Date parser. */
/** @param {string | null | undefined} iso @returns {number | null} */
export function parseTime(iso) {
  if (!iso) return null;
  const value = Date.parse(String(iso).replace(/(\.\d{3})\d+/, "$1"));
  return Number.isNaN(value) ? null : value;
}

const pad = (/** @type {number} */ n) => String(n).padStart(2, "0");

/** @param {number | null} ms */
export function formatClock(ms) {
  if (ms == null) return "—";
  const d = new Date(ms);
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

/** @param {number | null} ms */
export function formatDateTime(ms) {
  if (ms == null) return "—";
  const d = new Date(ms);
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${formatClock(ms)}`;
}

/** @param {number | null} ms @param {number} now */
export function formatRelative(ms, now) {
  if (ms == null) return "—";
  const seconds = Math.round((now - ms) / 1000);
  if (seconds < 5) return "刚刚";
  if (seconds < 60) return `${seconds} 秒前`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes} 分钟前`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} 小时前`;
  return `${Math.floor(hours / 24)} 天前`;
}

/** @param {number | null | undefined} ms */
export function formatDuration(ms) {
  if (ms == null || !Number.isFinite(ms)) return "—";
  if (ms < 1000) return `${Math.round(ms)} ms`;
  if (ms < 60000) return `${(ms / 1000).toFixed(1)} s`;
  const seconds = Math.round(ms / 1000);
  return `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`;
}

/** @param {string | null | undefined} hash */
export function shortHash(hash, head = 8, tail = 6) {
  if (!hash) return "—";
  return hash.length <= head + tail + 1 ? hash : `${hash.slice(0, head)}…${hash.slice(-tail)}`;
}

/** @param {string} hash */
export function groupHash(hash, size = 8) {
  const groups = [];
  for (let i = 0; i < hash.length; i += size) groups.push(hash.slice(i, i + size));
  return groups;
}

/** @param {number | null | undefined} n */
export function formatCount(n) {
  return typeof n === "number" && Number.isFinite(n) ? n.toLocaleString("zh-CN") : "—";
}
