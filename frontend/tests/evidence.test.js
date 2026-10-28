/* Parsing and trail-building checks on recorded-shape data. Not a browser or model run. */
import assert from "node:assert/strict";
import test from "node:test";
import { createRequire } from "node:module";
import { readFileSync } from "node:fs";
import { compareTests, parseUnittest, verdictCriteria, verificationEvidence } from "../src/lib/checks.js";
import { parseUnifiedDiff } from "../src/lib/diff.js";
import { formatDuration, parseTime } from "../src/lib/format.js";
import { runStopReason, runTone, stateLabel } from "../src/lib/labels.js";

test("durations never show 60 seconds and microsecond timestamps parse", () => {
  assert.equal(formatDuration(1139_700), "19 分 0 秒");
  assert.equal(formatDuration(61_000), "1 分 1 秒");
  assert.equal(formatDuration(1800), "1.8 s");
  assert.equal(parseTime("2026-10-28T03:00:01.123456+00:00"), Date.UTC(2026, 9, 28, 3, 0, 1, 123));
});
import { buildTrail, findBaseline, stageTrack } from "../src/lib/trail.js";

// Diff text shape recorded in artifacts/http-container-integration.json.
const DIFF = "--- a/src/quantities.py\n+++ b/src/quantities.py\n@@ -18,7 +18,7 @@\n     _validate(stock)\n     result = {}\n     for ingredient, amount in required.items():\n-        missing = amount if ingredient not in stock else 0\n+        missing = max(0, amount - stock.get(ingredient, 0))\n         if missing > 0:\n             result[ingredient] = missing\n     return result\n";

test("unified diff keeps old/new line numbers and counts", () => {
  const diff = parseUnifiedDiff(DIFF);
  assert.equal(diff.oldPath, "src/quantities.py");
  assert.equal(diff.added, 1);
  assert.equal(diff.removed, 1);
  const lines = diff.hunks[0].lines;
  assert.deepEqual(lines[3], { type: "del", text: "        missing = amount if ingredient not in stock else 0", oldNo: 21, newNo: null });
  assert.deepEqual(lines[4], { type: "add", text: "        missing = max(0, amount - stock.get(ingredient, 0))", oldNo: null, newNo: 21 });
  assert.equal(lines.at(-1).oldNo, 24);
});

const FAILED = "test_a (tests.t.C.test_a) ... FAIL\ntest_b (tests.t.C.test_b)\nDocstring line ... ok\n\n----------------------------------------------------------------------\nRan 2 tests in 0.001s\n\nFAILED (failures=1)\n";
const PASSED = "test_a (tests.t.C.test_a) ... ok\ntest_b (tests.t.C.test_b) ... ok\n\n----------------------------------------------------------------------\nRan 2 tests in 0.001s\n\nOK\n";

test("unittest output is lined up before and after, including docstring tests", () => {
  const before = parseUnittest(FAILED);
  assert.equal(before.ran, 2);
  assert.equal(before.summary, "FAILED (failures=1)");
  assert.deepEqual(compareTests(before, parseUnittest(PASSED)), [
    { name: "test_a", before: "fail", after: "ok" },
    { name: "test_b", before: "ok", after: "ok" },
  ]);
});

test("verdict criteria mirror the server rule and flag each failed condition", () => {
  const ok = verdictCriteria({ exit_code: 0, output: PASSED, timed_out: false, truncated: false, tests_run: 2 });
  assert.ok(ok.every((c) => c.ok));
  const zero = verdictCriteria({ exit_code: 0, output: "Ran 0 tests in 0s\n\nOK\n", timed_out: false, truncated: false, tests_run: 0 });
  assert.deepEqual(zero.filter((c) => !c.ok).map((c) => c.key), ["tests"]);
  const timeout = verdictCriteria({ exit_code: null, output: "", timed_out: true, truncated: false, tests_run: 0 });
  assert.deepEqual(timeout.filter((c) => !c.ok).map((c) => c.key), ["exit", "tests", "timeout", "ok"]);
});

test("completion evidence separates complete test failures from missing or partial checks", () => {
  const ok = { exit_code: 0, output: PASSED, timed_out: false, truncated: false, tests_run: 2, passed: true };
  const failed = { ...ok, exit_code: 1, output: FAILED, passed: false };
  assert.equal(verificationEvidence(ok).complete, true);
  assert.equal(verificationEvidence(failed).complete, true);
  const incomplete = [
    { ...failed, exit_code: 125, output: "Error: image is not known\n", tests_run: 0 },
    { ...ok, output: "Ran 0 tests in 0s\n\nOK\n", tests_run: 0 },
    { ...failed, timed_out: true },
    { ...failed, truncated: true },
    { ...failed, output: "test_a (tests.t.C.test_a) ... FAIL\n", tests_run: 1 },
    { ...ok, exit_code: 1 },
    { ...ok, tests_run: 9 },
  ];
  for (const result of incomplete) {
    const evidence = verificationEvidence(result);
    assert.equal(evidence.complete, false);
    assert.ok(evidence.reason);
  }
  assert.match(verificationEvidence({ ...failed, timed_out: true }).reason, /可能只执行了一部分/);
});

test("legacy verifier_failed records without completion evidence remain warnings", () => {
  const legacy = { state: "failed", stop_reason: "verifier_failed", result: { exit_code: 125, output: "image absent", tests_run: 0, timed_out: false, truncated: false } };
  assert.equal(runStopReason(legacy).unconfirmed, true);
  assert.equal(runTone(legacy), "warning");
  assert.equal(stateLabel(legacy), "验证未确认");
  assert.equal(runStopReason({ state: "failed", stop_reason: "verification_unconfirmed" }).unconfirmed, true);
  assert.equal(runTone({ ...legacy, result: { exit_code: 1, output: FAILED, tests_run: 2, timed_out: false, truncated: false } }), "danger");
});

test("actual CheckPanel SSR does not call zero-test or partial results a patch failure", async () => {
  // Component regression, not a browser/container/model run. Requires npm ci.
  const require = createRequire(import.meta.url);
  const ts = require("typescript");
  const React = require("react");
  const { renderToStaticMarkup } = require("react-dom/server");
  const modules = {
    "../lib/checks.js": await import("../src/lib/checks.js"),
    "../lib/format.js": await import("../src/lib/format.js"),
    "../lib/labels.js": await import("../src/lib/labels.js"),
    "../lib/trail.js": await import("../src/lib/trail.js"),
    "./ui": { Icon: () => null },
  };
  const source = readFileSync(new URL("../src/components/CheckPanel.tsx", import.meta.url), "utf8");
  const code = ts.transpileModule(source, { compilerOptions: {
    module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, esModuleInterop: true,
  } }).outputText;
  const module = { exports: {} };
  new Function("require", "module", "exports", code)((name) => modules[name] ?? require(name), module, module.exports);
  const render = (result, stopReason = "verifier_failed") => renderToStaticMarkup(React.createElement(module.exports.CheckPanel, {
    run: { state: "failed", stop_reason: stopReason, result }, events: [],
  }));
  for (const result of [
    { exit_code: 125, output: "Error: image is not known\n", tests_run: 0, timed_out: false, truncated: false, passed: false, runner: "podman", duration_ms: 1 },
    { exit_code: 125, output: "Error: image is not known\n", tests_run: 0, timed_out: false, truncated: false, passed: true, runner: "podman", duration_ms: 1 },
    { exit_code: 1, output: FAILED, tests_run: 2, timed_out: true, truncated: false, passed: false, runner: "podman", duration_ms: 1000 },
  ]) {
    const html = render(result);
    assert.match(html, /检查未确认完成，不能判断补丁/);
    assert.match(html, /verdict tone-warning/);
    assert.doesNotMatch(html, /verdict tone-danger/);
    assert.doesNotMatch(html, /补丁未被执行/);
  }
  const failed = render({ exit_code: 1, output: FAILED, tests_run: 2, timed_out: false, truncated: false, passed: false, runner: "podman", duration_ms: 1 });
  assert.match(failed, /验证未通过/);
  assert.match(failed, /verdict tone-danger/);
  const fake = render({ exit_code: 0, output: PASSED, tests_run: 2, timed_out: false, truncated: false, passed: true, runner: "design-harness-fake", duration_ms: 1 });
  assert.match(fake, /非容器执行器/);
  assert.match(fake, /结果不是证据/);
  const withoutOutput = render({ error: "check completion unconfirmed" }, "verification_unconfirmed");
  assert.match(withoutOutput, /verdict tone-warning/);
  assert.doesNotMatch(withoutOutput, /补丁未被执行/);
});

const at = (s) => `2026-10-28T03:00:${String(s).padStart(2, "0")}.123456+00:00`;
const EVENTS = [
  { id: 1, kind: "run_created", payload: { mode: "demo" }, call_id: null, created_at: at(0) },
  { id: 2, kind: "state_changed", payload: { from: "queued", to: "diagnosing", version: 1 }, call_id: null, created_at: at(1) },
  { id: 3, kind: "state_changed", payload: { from: "diagnosing", to: "diagnosing", version: 2 }, call_id: null, created_at: at(1) },
  { id: 4, kind: "model_requested", payload: { call: 1, provider: "Scripted" }, call_id: null, created_at: at(2) },
  { id: 5, kind: "model_response", payload: { call: 1, content: "", tool_names: ["run_check"] }, call_id: null, created_at: at(2) },
  { id: 6, kind: "tool_requested", payload: { name: "run_check", arguments: { check_id: "tests" }, model_call: 1 }, call_id: "1:c1", created_at: at(3) },
  { id: 7, kind: "tool_result", payload: { name: "run_check", result: { exit_code: 1, output: FAILED }, model_call: 1 }, call_id: "1:c1", created_at: at(5) },
  { id: 8, kind: "tool_rejected", payload: { name: "run_check", error: "Repeated identical tool call" }, call_id: "1:c2", created_at: at(5) },
  { id: 9, kind: "state_changed", payload: { from: "diagnosing", to: "awaiting_approval", version: 3 }, call_id: null, created_at: at(6) },
  { id: 10, kind: "state_changed", payload: { from: "awaiting_approval", to: "verifying", version: 4 }, call_id: null, created_at: at(9) },
  { id: 11, kind: "brand_new_kind", payload: { x: 1 }, call_id: null, created_at: at(9) },
  { id: 12, kind: "state_changed", payload: { from: "verifying", to: "interrupted", version: 5, reason: "process_restart" }, call_id: null, created_at: at(10) },
];

test("trail nests tools in their model round, counts checkpoints and keeps unknown events", () => {
  const { items, checkpoints } = buildTrail(EVENTS);
  assert.equal(checkpoints, 1);
  const round = items.find((i) => i.kind === "round");
  assert.equal(round.response.tool_names[0], "run_check");
  assert.equal(round.tools.length, 2);
  assert.equal(round.tools[0].result.exit_code, 1);
  assert.match(round.tools[1].error, /Repeated/);
  assert.ok(items.some((i) => i.kind === "other" && i.event.kind === "brand_new_kind"));
  assert.equal(findBaseline(EVENTS).exit_code, 1);
});

test("stage track marks where an interrupted or failed run stopped", () => {
  const interrupted = stageTrack({ state: "interrupted", created_at: at(0), proposal: {} }, EVENTS, Date.parse(at(20)));
  assert.deepEqual(interrupted.map((s) => s.status), ["done", "done", "done", "stopped", "stopped"]);
  assert.equal(interrupted[1].duration, 5000);
  const verifierFailed = stageTrack({ state: "failed", stop_reason: "verifier_failed", created_at: at(0), proposal: {},
    result: { exit_code: 1, output: FAILED, tests_run: 2, timed_out: false, truncated: false } },
    [...EVENTS.slice(0, 10), { id: 13, kind: "state_changed", payload: { from: "verifying", to: "failed" }, call_id: null, created_at: at(12) }], 0);
  assert.deepEqual(verifierFailed.map((s) => s.status), ["done", "done", "done", "done", "stopped"]);
  const active = stageTrack({ state: "diagnosing", created_at: at(0) }, EVENTS.slice(0, 4), Date.parse(at(4)));
  assert.deepEqual(active.map((s) => s.status), ["done", "current", "pending", "pending", "pending"]);
  assert.equal(active[1].duration, 3000);
});

test("an unconfirmed legacy result does not mark the verification stage completed", () => {
  const events = [...EVENTS.slice(0, 10), { id: 13, kind: "state_changed", payload: { from: "verifying", to: "failed" }, call_id: null, created_at: at(12) }];
  for (const stop_reason of ["verifier_failed", "verification_unconfirmed"]) {
    const result = stageTrack({ state: "failed", stop_reason, created_at: at(0), proposal: {},
      result: { exit_code: 125, output: "image absent", tests_run: 0, timed_out: false, truncated: false } }, events, 0);
    assert.deepEqual(result.map((stage) => stage.status), ["done", "done", "done", "stopped", "stopped"]);
  }
});
