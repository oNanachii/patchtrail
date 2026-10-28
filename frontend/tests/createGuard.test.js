/* Contract-level create/retry state test with explicit fetch and storage doubles.
   This is not a browser or model run. */
import assert from "node:assert/strict";
import test from "node:test";
import { ApiError, createApi } from "../src/lib/api.js";
import { PENDING_KEY, classifyCreateFailure, pendingStore, submitCreate } from "../src/lib/createGuard.js";
import { createAuthEpoch } from "../src/lib/authEpoch.js";

const RUN = { id: "a".repeat(32), task_id: "quantity-shortfall", mode: "demo", state: "queued", version: 0 };
const SELECTION = { task_id: "quantity-shortfall", mode: "demo" };

function memoryStorage() {
  const map = new Map();
  return { map, getItem: (k) => map.get(k) ?? null, setItem: (k, v) => map.set(k, String(v)), removeItem: (k) => map.delete(k) };
}

/** A server double that records every create request and answers from a script. */
function server(script) {
  const requests = [];
  const fetchImpl = async (url, init) => {
    assert.equal(url, "/api/runs");
    requests.push({ key: init.headers["Idempotency-Key"], body: JSON.parse(init.body), auth: init.headers.Authorization ?? null });
    const step = script.shift() ?? { status: 201, body: RUN };
    if (step.throws) throw new TypeError("response lost after commit");
    return { ok: step.status < 300, status: step.status, json: async () => step.body };
  };
  return { requests, fetchImpl };
}

let counter = 0;
const newKey = () => `request-${++counter}`;

test("lost response keeps the request; a reload reconciles with the same key and body", async () => {
  const storage = memoryStorage();
  const { requests, fetchImpl } = server([{ throws: true }]);
  const request = createApi({ fetchImpl, getToken: () => "" });

  await assert.rejects(submitCreate({ store: pendingStore(storage), request, selection: SELECTION, newKey }), (error) => {
    assert.equal(error.createOutcome, "unknown");
    return true;
  });
  assert.ok(storage.map.has(PENDING_KEY), "pending request survives in sessionStorage");

  // A fresh store simulates a page reload; the user now has a different task selected.
  const reloaded = pendingStore(storage);
  const { run, reused } = await submitCreate({ store: reloaded, request, selection: { task_id: "batch-expiry", mode: "live" }, newKey });
  assert.equal(run.id, RUN.id);
  assert.equal(reused, true);
  assert.equal(requests.length, 2);
  assert.equal(requests[0].key, requests[1].key);
  assert.deepEqual(requests[1].body, SELECTION, "the original body is resent, not the new selection");
  assert.equal(storage.map.has(PENDING_KEY), false);
  assert.equal(reloaded.get(), null);
});

test("a response proving no run exists clears the record and allows a new key", async () => {
  for (const [status, code] of [[422, "invalid_request"], [503, "sandbox_unavailable"], [503, "model_not_configured"], [429, "queue_full"], [429, "rate_limited"], [404, "unknown_task"]]) {
    const storage = memoryStorage();
    const store = pendingStore(storage);
    const { requests, fetchImpl } = server([{ status, body: { error: { code, message: "x" } } }, { status: 201, body: RUN }]);
    const request = createApi({ fetchImpl, getToken: () => "" });
    await assert.rejects(submitCreate({ store, request, selection: SELECTION, newKey }), (error) => error.createOutcome === "rejected");
    assert.equal(store.get(), null, `${code} clears the pending request`);
    await submitCreate({ store, request, selection: SELECTION, newKey });
    assert.notEqual(requests[0].key, requests[1].key, `${code} allows a genuinely new request`);
  }
});

test("a retry rejection cannot erase an unknown request whose original may commit later", async () => {
  for (const [status, code] of [[429, "rate_limited"], [429, "queue_full"], [503, "sandbox_unavailable"], [503, "model_not_configured"]]) {
    const storage = memoryStorage();
    const store = pendingStore(storage);
    const { requests, fetchImpl } = server([
      { throws: true }, // Client lost the response while the original server operation is still in flight.
      { status, body: { error: { code, message: "retry arrived before the original committed" } } },
      { status: 201, body: { ...RUN, mode: "live" } }, // Reconcile the original after it committed.
    ]);
    const request = createApi({ fetchImpl, getToken: () => "unit-operator" });
    const selection = { task_id: "quantity-shortfall", mode: "live" };
    await assert.rejects(submitCreate({ store, request, selection, newKey }));
    const pending = store.get();
    await assert.rejects(submitCreate({ store, request, selection, newKey }), (error) => error.createOutcome === "unknown");
    assert.deepEqual(store.get(), pending, `${code} does not prove the first operation cannot still commit`);
    await submitCreate({ store, request, selection, newKey });
    assert.equal(new Set(requests.map((item) => item.key)).size, 1);
    assert.equal(store.get(), null);
  }
});

test("an existing unknown key conflict is retained for operator reconciliation", async () => {
  const storage = memoryStorage();
  const store = pendingStore(storage);
  const { fetchImpl } = server([{ throws: true }, { status: 409, body: { error: { code: "state_conflict", message: "key already exists" } } }]);
  const request = createApi({ fetchImpl, getToken: () => "" });
  await assert.rejects(submitCreate({ store, request, selection: SELECTION, newKey }));
  const pending = store.get();
  await assert.rejects(submitCreate({ store, request, selection: SELECTION, newKey }), (error) => error.createOutcome === "key_conflict");
  assert.deepEqual(store.get(), pending);
});

test("missing operator authentication keeps the record: an earlier attempt may have committed", async () => {
  const storage = memoryStorage();
  const store = pendingStore(storage);
  let token = "";
  const { requests, fetchImpl } = server([
    { throws: true },
    { status: 401, body: { error: { code: "operator_required", message: "Operator authentication is required" } } },
    { status: 201, body: { ...RUN, mode: "live" } },
  ]);
  const request = createApi({ fetchImpl, getToken: () => token });
  const live = { task_id: "quantity-shortfall", mode: "live" };
  token = "operator-token";
  await assert.rejects(submitCreate({ store, request, selection: live, newKey }));
  token = ""; // Reload drops the in-memory token.
  await assert.rejects(submitCreate({ store, request, selection: live, newKey }), (error) => error.createOutcome === "needs_operator");
  assert.ok(store.get(), "401 does not clear the pending live request");
  token = "operator-token";
  await submitCreate({ store, request, selection: SELECTION, newKey });
  assert.deepEqual(new Set(requests.map((r) => r.key)).size, 1, "all three attempts used one key");
  assert.equal(requests[2].auth, "Bearer operator-token");
  assert.equal(store.get(), null);
});

test("key conflicts and malformed success bodies are classified, never silently retried with a new key", async () => {
  assert.equal(classifyCreateFailure(new ApiError({ status: 409, code: "state_conflict", message: "" })), "key_conflict");
  assert.equal(classifyCreateFailure(new ApiError({ status: 502, code: "http_502", message: "" })), "unknown");
  assert.equal(classifyCreateFailure(new ApiError({ status: 500, code: "http_500", message: "" })), "unknown");
  assert.equal(classifyCreateFailure(new ApiError({ status: 503, code: "operator_not_configured", message: "" })), "needs_operator");
  // Busy storage may have committed nothing or everything; the original key must be reused.
  assert.equal(classifyCreateFailure(new ApiError({ status: 503, code: "storage_busy", message: "" })), "unknown");

  const storage = memoryStorage();
  const store = pendingStore(storage);
  const { fetchImpl } = server([{ status: 201, body: { unexpected: true } }]);
  await assert.rejects(submitCreate({ store, request: createApi({ fetchImpl, getToken: () => "" }), selection: SELECTION, newKey }),
    (error) => error.createOutcome === "unknown");
  assert.ok(store.get(), "an unconfirmable success body keeps the request");
});

test("blocked storage still protects the current page, and the token is never stored", async () => {
  const blocked = { getItem() { throw new Error("blocked"); }, setItem() { throw new Error("blocked"); }, removeItem() { throw new Error("blocked"); } };
  const store = pendingStore(blocked);
  const { requests, fetchImpl } = server([{ throws: true }, { status: 201, body: RUN }]);
  const request = createApi({ fetchImpl, getToken: () => "secret-operator-token" });
  await assert.rejects(submitCreate({ store, request, selection: SELECTION, newKey }));
  await submitCreate({ store, request, selection: SELECTION, newKey });
  assert.equal(requests[0].key, requests[1].key);

  const storage = memoryStorage();
  const tracked = pendingStore(storage);
  const second = server([{ throws: true }]);
  await assert.rejects(submitCreate({ store: tracked, request: createApi({ fetchImpl: second.fetchImpl, getToken: () => "secret-operator-token" }), selection: SELECTION, newKey }));
  assert.ok(!storage.map.get(PENDING_KEY).includes("secret-operator-token"));
});

test("non-2xx responses become ApiError with the server's code; fetch failures are network errors", async () => {
  const request = createApi({ fetchImpl: async () => ({ ok: false, status: 409, json: async () => ({ error: { code: "state_conflict", message: "Run version changed" } }) }), getToken: () => "" });
  await assert.rejects(request("/api/runs/x/approve", { method: "POST", body: {} }), (error) => error instanceof ApiError && error.status === 409 && error.code === "state_conflict");
  const offline = createApi({ fetchImpl: async () => { throw new TypeError("Failed to fetch"); }, getToken: () => "" });
  await assert.rejects(offline("/api/health"), (error) => error.network === true && error.status === 0);
  const html = createApi({ fetchImpl: async () => ({ ok: false, status: 502, json: async () => { throw new SyntaxError("<html>"); } }), getToken: () => "" });
  await assert.rejects(html("/api/health"), (error) => error.code === "http_502");
});

test("credential changes discard a buffered old success before it can repopulate a cache", async () => {
  const epoch = createAuthEpoch();
  const ticket = epoch.capture();
  let resolve;
  const response = new Promise((done) => { resolve = done; });
  const pending = epoch.guard(ticket, () => response);
  let cache = null;
  const applied = pending.then((value) => { cache = value; });
  epoch.advance(); // The UI clears its private run/event cache synchronously here.
  resolve({ privateDiff: "previous credential data" });
  await assert.rejects(applied, (error) => error.name === "AbortError");
  assert.equal(cache, null);
});

test("an old401 cannot reject a newly loaded credential", async () => {
  const epoch = createAuthEpoch();
  const ticket = epoch.capture();
  let reject;
  const response = new Promise((_, fail) => { reject = fail; });
  const pending = epoch.guard(ticket, () => response);
  epoch.advance();
  reject(new ApiError({ status: 401, code: "operator_required", message: "old credential denied" }));
  await assert.rejects(pending, (error) => error.name === "AbortError" && error.status === undefined);
  assert.equal(await epoch.guard(epoch.capture(), async () => "current role result"), "current role result");
});

test("current credential failures stay actionable rather than being swallowed", async () => {
  const epoch = createAuthEpoch();
  const failure = new ApiError({ status: 401, code: "operator_required", message: "current credential denied" });
  await assert.rejects(epoch.guard(epoch.capture(), async () => { throw failure; }), (error) => error === failure);
});
