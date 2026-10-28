// @ts-check
/**
 * Idempotent run creation that survives lost responses and page reloads.
 *
 * A create request is recorded (key + exact body, never the token) before it is sent.
 * It is cleared only after the server returns the run, or after a response proving that
 * no run exists for that key. Any other outcome keeps the record, so the next attempt
 * resends the same Idempotency-Key and body and the server returns the original run
 * instead of starting a second (possibly paid) job.
 */

export const PENDING_KEY = "patchtrail.pendingStart";

/**
 * @typedef {{task_id: string, mode: "demo" | "live"}} CreateBody
 * @typedef {{key: string, body: CreateBody}} PendingCreate
 * @typedef {{get(): PendingCreate | null, set(value: PendingCreate | null): void}} PendingStore
 * @typedef {"rejected" | "needs_operator" | "key_conflict" | "unknown"} CreateOutcome
 */

/** @param {unknown} value @returns {PendingCreate | null} */
export function validPending(value) {
  if (!value || typeof value !== "object") return null;
  const { key, body } = /** @type {any} */ (value);
  if (typeof key !== "string" || key.length < 8 || key.length > 128) return null;
  if (!body || typeof body.task_id !== "string" || !["demo", "live"].includes(body.mode)) return null;
  return { key, body: { task_id: body.task_id, mode: body.mode } };
}

/**
 * sessionStorage-backed store with an in-memory mirror for when storage is blocked.
 * @param {Pick<Storage, "getItem" | "setItem" | "removeItem"> | null} storage
 * @returns {PendingStore}
 */
export function pendingStore(storage) {
  /** @type {PendingCreate | null} */
  let memory = null;
  try {
    memory = validPending(JSON.parse(storage?.getItem(PENDING_KEY) ?? "null"));
  } catch {
    memory = null;
  }
  return {
    get: () => memory,
    set(value) {
      memory = value;
      try {
        if (value) storage?.setItem(PENDING_KEY, JSON.stringify(value));
        else storage?.removeItem(PENDING_KEY);
      } catch {
        // Storage can be blocked; the in-memory copy still protects this page.
      }
    },
  };
}

// A fresh request rejected by these checks did not create a run. A retry cannot
// make that claim: its original request may still commit after this response.
const REJECTED_AFTER_LOOKUP = new Set(["sandbox_unavailable", "model_not_configured", "queue_full", "rate_limited"]);

/**
 * @param {any} error ApiError-like ({status, code, network})
 * @returns {CreateOutcome}
 */
export function classifyCreateFailure(error) {
  if (!error || error.network || typeof error.status !== "number" || error.status === 0) return "unknown";
  const { status, code } = error;
  // Authentication runs before the key lookup: an earlier attempt may still have committed.
  if (status === 401 || code === "operator_required" || code === "operator_not_configured") return "needs_operator";
  if (status === 409) return "key_conflict";
  if (REJECTED_AFTER_LOOKUP.has(code)) return "rejected";
  // Validation failures would have rejected the identical original body as well.
  if (status >= 400 && status < 500) return "rejected";
  return "unknown";
}

/**
 * @param {{store: PendingStore, request: import("./api.js").Request, selection: CreateBody, newKey: () => string}} options
 * @returns {Promise<{run: any, reused: boolean}>}
 */
export async function submitCreate({ store, request, selection, newKey }) {
  const existing = store.get();
  const pending = existing ?? { key: newKey(), body: { task_id: selection.task_id, mode: selection.mode } };
  if (!existing) store.set(pending);
  try {
    const run = await request("/api/runs", {
      method: "POST",
      headers: { "Idempotency-Key": pending.key },
      body: pending.body,
    });
    if (!run || typeof run.id !== "string" || !run.id) {
      throw Object.assign(new Error("创建结果无法确认"), { status: 0, code: "invalid_response", network: true });
    }
    store.set(null);
    return { run, reused: Boolean(existing) };
  } catch (error) {
    let outcome = classifyCreateFailure(error);
    if (existing && outcome === "rejected") outcome = "unknown";
    if (!existing && (outcome === "rejected" || outcome === "key_conflict")) store.set(null);
    throw Object.assign(/** @type {object} */ (error), { createOutcome: outcome, pending });
  }
}
