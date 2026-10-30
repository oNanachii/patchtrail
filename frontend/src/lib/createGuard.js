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
 * @typedef {{task_id: string, mode: "demo" | "live", preflight_id?: string}} CreateBody
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
  if (body.preflight_id !== undefined && !/^[a-f0-9]{32}$/.test(body.preflight_id)) return null;
  return { key, body: { task_id: body.task_id, mode: body.mode, ...(body.preflight_id ? {preflight_id:body.preflight_id} : {}) } };
}

/**
 * sessionStorage-backed store with an in-memory mirror for when storage is blocked.
 * @param {Pick<Storage, "getItem" | "setItem" | "removeItem"> | null} storage
 * @param {string | null} scope
 * @returns {PendingStore}
 */
export function pendingStore(storage, scope = null) {
  /** @type {PendingCreate | null} */
  let memory = null;
  const storageKey = scope === null ? PENDING_KEY : `${PENDING_KEY}.${scope}`;
  try {
    memory = validPending(JSON.parse(storage?.getItem(storageKey) ?? "null"));
  } catch {
    memory = null;
  }
  function adoptLegacy() {
    if (memory || !storage || !["operator", "public"].includes(scope ?? "")) return;
    try {
      const legacy = validPending(JSON.parse(storage.getItem(PENDING_KEY) ?? "null"));
      if (legacy && legacy.body.mode === (scope === "operator" ? "live" : "demo")) {
        memory = legacy;
        storage.setItem(storageKey, JSON.stringify(legacy));
        storage.removeItem(PENDING_KEY);
      }
    } catch { /* The in-memory request still protects this page if storage is blocked. */ }
  }
  adoptLegacy();
  return {
    get: () => memory,
    set(value) {
      memory = value;
      try {
        if (value) storage?.setItem(storageKey, JSON.stringify(value));
        else storage?.removeItem(storageKey);
      } catch {
        // Storage can be blocked; the in-memory copy still protects this page.
      }
      if (!value) adoptLegacy();
    },
  };
}

// A fresh request rejected by these checks did not create a run. A retry cannot
// make that claim: its original request may still commit after this response.
const REJECTED_AFTER_LOOKUP = new Set(["sandbox_unavailable", "model_not_configured", "queue_full", "rate_limited", "member_quota", "preflight_required"]);

/**
 * @param {any} error ApiError-like ({status, code, network})
 * @returns {CreateOutcome}
 */
export function classifyCreateFailure(error) {
  if (!error || error.network || typeof error.status !== "number" || error.status === 0) return "unknown";
  const { status, code } = error;
  // Authentication runs before the key lookup: an earlier attempt may still have committed.
  if (status === 401 || code === "operator_required" || code === "operator_not_configured") return "needs_operator";
  if (REJECTED_AFTER_LOOKUP.has(code)) return "rejected";
  if (status === 409) return "key_conflict";
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
  const pending = existing ?? { key: newKey(), body: { task_id: selection.task_id, mode: selection.mode, ...(selection.preflight_id ? {preflight_id:selection.preflight_id} : {}) } };
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

/** Same-page fallback retains only reconciliation metadata across identity switches.
 * @param {Pick<Storage, "getItem" | "setItem" | "removeItem"> | null} storage
 */
export function createPendingRegistry(storage) {
  /** @type {Map<string,PendingStore>} */
  const stores = new Map();
  return {
    /** @param {string} scope */
    forScope(scope) {
      let store = stores.get(scope);
      if (!store) { store = pendingStore(storage,scope); stores.set(scope,store); }
      return store;
    },
  };
}
