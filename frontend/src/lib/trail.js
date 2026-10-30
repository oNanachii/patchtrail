// @ts-check
import { parseTime } from "./format.js";
import { verificationEvidence } from "./checks.js";

/**
 * Turns the flat event log into a readable evidence trail: model rounds own the tool calls
 * they requested, same-state checkpoint saves are counted rather than listed, and unknown
 * event kinds are kept (never dropped) so nothing in the record is hidden.
 *
 * @typedef {{id: number, kind: string, payload: any, call_id: string | null, created_at: string}} RunEvent
 * @typedef {{key: string, name: string, args: any, result: any, error: string | null, at: number | null, resultAt: number | null}} ToolCall
 * @typedef {{kind: "round", id: number, at: number | null, call: number, provider: string, response: any, tools: ToolCall[]}} Round
 * @typedef {{kind: "created" | "stage" | "check_unconfirmed" | "verification" | "resume_unknown" | "other", id: number, at: number | null, payload: any, event: RunEvent}} Marker
 * @typedef {{kind: "tool", id: number, at: number | null, tool: ToolCall}} LooseTool
 * @typedef {Round | Marker | LooseTool} TrailItem
 */

const MARKERS = {
  run_created: "created",
  state_changed: "stage",
  check_unconfirmed: "check_unconfirmed",
  verification_started: "verification",
  explicit_resume_after_unknown_model_outcome: "resume_unknown",
};

/** @param {RunEvent[]} events */
export function buildTrail(events) {
  /** @type {TrailItem[]} */
  const items = [];
  /** @type {Map<number, Round>} */
  const rounds = new Map();
  /** @type {Map<string, ToolCall>} */
  const tools = new Map();
  /** @type {Round | null} */
  let lastRound = null;
  let checkpoints = 0;
  for (const event of events) {
    const at = parseTime(event.created_at);
    const p = event.payload ?? {};
    if (event.kind === "state_changed" && p.from === p.to) {
      checkpoints++;
      continue;
    }
    if (event.kind === "model_requested") {
      /** @type {Round} */
      const round = { kind: "round", id: event.id, at, call: p.call, provider: p.provider ?? "", response: null, tools: [] };
      rounds.set(p.call, round);
      lastRound = round;
      items.push(round);
      continue;
    }
    if (event.kind === "model_response" && rounds.has(p.call)) {
      /** @type {Round} */ (rounds.get(p.call)).response = p;
      continue;
    }
    if (event.kind === "tool_requested" || event.kind === "tool_result" || event.kind === "tool_rejected") {
      const key = event.call_id ?? `${event.kind}:${event.id}`;
      let tool = tools.get(key);
      if (!tool) {
        tool = { key, name: p.name ?? "unknown", args: null, result: null, error: null, at, resultAt: null };
        tools.set(key, tool);
        const owner = (typeof p.model_call === "number" && rounds.get(p.model_call)) || lastRound;
        if (owner) owner.tools.push(tool);
        else items.push({ kind: "tool", id: event.id, at, tool });
      }
      if (event.kind === "tool_requested") tool.args = p.arguments ?? null;
      if (event.kind === "tool_result") {
        tool.result = p.result ?? null;
        tool.resultAt = at;
        if (p.result && typeof p.result.error === "string") tool.error = p.result.error;
      }
      if (event.kind === "tool_rejected") {
        tool.error = typeof p.error === "string" ? p.error : "工具调用被拒绝";
        tool.resultAt = at;
      }
      continue;
    }
    const kind = /** @type {Marker["kind"]} */ (MARKERS[/** @type {keyof typeof MARKERS} */ (event.kind)] ?? "other");
    items.push({ kind, id: event.id, at, payload: p, event });
  }
  return { items, checkpoints };
}

/**
 * The single baseline check (identical tool calls are rejected, so run_check runs once).
 * @param {RunEvent[]} events
 */
export function findBaseline(events) {
  for (const event of events) {
    if (event.kind === "baseline_checked" && typeof event.payload?.check?.output === "string") return event.payload.check;
    if (event.kind === "tool_result" && event.payload?.name === "run_check" && typeof event.payload?.result?.output === "string") {
      return event.payload.result;
    }
  }
  return null;
}

/** @param {RunEvent[]} events */
export function findVerification(events) {
  return events.findLast((event) => event.kind === "verification_started")?.payload ?? null;
}

/** @param {RunEvent[]} events */
export function findCreated(events) {
  return events.find((event) => event.kind === "run_created")?.payload ?? null;
}

export const STAGES = /** @type {const} */ (["queued", "diagnosing", "awaiting_approval", "verifying", "outcome"]);
const STOPPED = new Set(["failed", "cancelled", "interrupted"]);

/**
 * @param {{state: string, created_at: string, updated_at?: string, proposal?: any, stop_reason?: string | null, result?: any}} run
 * @param {RunEvent[]} events
 * @param {number} now
 */
export function stageTrack(run, events, now) {
  /** @type {Record<string, number | null>} */
  const entered = { queued: parseTime(run.created_at) };
  let stopFrom = null;
  for (const event of events) {
    if (event.kind !== "state_changed") continue;
    const { from, to } = event.payload ?? {};
    if (from === to) continue;
    entered[to] = parseTime(event.created_at);
    if (to === run.state) stopFrom = from;
  }
  if (run.state === "succeeded") entered.outcome = entered.succeeded ?? parseTime(run.updated_at);
  if (STOPPED.has(run.state)) entered.outcome = entered[run.state] ?? parseTime(run.updated_at);

  let reached;
  if (run.state === "succeeded") reached = STAGES.length;
  else if (STOPPED.has(run.state)) {
    const fallback = run.proposal ? "awaiting_approval" : "diagnosing";
    const from = STAGES.includes(/** @type {any} */ (stopFrom)) ? stopFrom : fallback;
    reached = STAGES.indexOf(/** @type {any} */ (from));
    // The verifier finished and gave its verdict: verification itself completed.
    if (run.stop_reason === "verifier_failed" && from === "verifying" && verificationEvidence(run.result).complete) reached += 1;
  } else reached = STAGES.indexOf(/** @type {any} */ (run.state));

  return STAGES.map((key, index) => {
    /** @type {"done" | "current" | "pending" | "stopped" | "skipped"} */
    let status;
    if (run.state === "succeeded") status = "done";
    else if (STOPPED.has(run.state)) {
      if (key === "outcome") status = "stopped";
      else if (index < reached) status = "done";
      else if (index === reached) status = "stopped";
      else status = "skipped";
    } else status = index < reached ? "done" : index === reached ? "current" : "pending";
    const start = status === "pending" || status === "skipped" ? null : entered[key] ?? null;
    const nextKey = STAGES.slice(index + 1).find((k) => entered[k] != null && k !== "outcome");
    let end = null;
    if (status === "current") end = now;
    else if (status === "done" || status === "stopped") end = (nextKey && entered[nextKey]) ?? entered.outcome ?? null;
    const duration = key !== "outcome" && start != null && end != null && end >= start ? end - start : null;
    return { key, status, at: start, duration };
  });
}
