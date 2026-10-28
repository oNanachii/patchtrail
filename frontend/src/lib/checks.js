// @ts-check
/**
 * Reading verifier output. The server decides pass/fail; these helpers only explain
 * that decision and line up per-test results from the baseline and verification runs.
 * @typedef {{name: string, id: string, status: string}} TestLine
 * @typedef {{tests: TestLine[], ran: number | null, summary: string | null}} ParsedOutput
 */

const TEST_LINE = /^(test\w*) \(([^)]+)\)(?: \.\.\. (.+))?$/;
const STATUS_TAIL = / \.\.\. (ok|FAIL|ERROR|skipped.*|expected failure|unexpected success)$/;

/** @param {string} output @returns {ParsedOutput} */
export function parseUnittest(output) {
  /** @type {TestLine[]} */
  const tests = [];
  /** @type {{name: string, id: string} | null} */
  let waiting = null;
  let ran = null;
  let summary = null;
  for (const line of (output ?? "").split("\n")) {
    const match = TEST_LINE.exec(line);
    if (match) {
      if (match[3]) tests.push({ name: match[1], id: match[2], status: normalize(match[3]) });
      else waiting = { name: match[1], id: match[2] };
      continue;
    }
    if (waiting) {
      // unittest prints a docstring line before " ... status" when a test has one.
      const tail = STATUS_TAIL.exec(line);
      if (tail) {
        tests.push({ ...waiting, status: normalize(tail[1]) });
        waiting = null;
        continue;
      }
    }
    const count = /^Ran (\d+) tests? in /.exec(line);
    if (count) ran = Number(count[1]);
    if (/^(OK|FAILED)\b/.test(line)) summary = line.trim();
  }
  return { tests, ran, summary };
}

/** @param {string} status */
function normalize(status) {
  if (status === "ok") return "ok";
  if (status === "FAIL") return "fail";
  if (status === "ERROR") return "error";
  if (status.startsWith("skipped")) return "skipped";
  return status;
}

/**
 * @param {ParsedOutput | null} before
 * @param {ParsedOutput | null} after
 * @returns {{name: string, before: string | null, after: string | null}[]}
 */
export function compareTests(before, after) {
  /** @type {Map<string, {name: string, before: string | null, after: string | null}>} */
  const rows = new Map();
  for (const test of before?.tests ?? []) rows.set(test.name, { name: test.name, before: test.status, after: null });
  for (const test of after?.tests ?? []) {
    const row = rows.get(test.name) ?? { name: test.name, before: null, after: null };
    row.after = test.status;
    rows.set(test.name, row);
  }
  return [...rows.values()];
}

/**
 * Mirrors CheckResult.passed in patchtrail/sandbox.py so the UI can show which
 * condition decided the outcome. The server's own `passed` flag stays authoritative.
 * @param {any} result
 */
export function verdictCriteria(result) {
  const output = typeof result?.output === "string" ? result.output : "";
  const lastLine = output.trimEnd().split("\n").at(-1) ?? "";
  const testsRun = typeof result?.tests_run === "number" ? result.tests_run : parseUnittest(output).ran ?? 0;
  return [
    { key: "exit", label: "退出码为 0", ok: result?.exit_code === 0, detail: result?.exit_code == null ? "无退出码" : `exit ${result.exit_code}` },
    { key: "tests", label: "实际运行了测试", ok: testsRun > 0, detail: `${testsRun} 个用例` },
    { key: "timeout", label: "未超时", ok: result?.timed_out === false, detail: result?.timed_out ? "已超时" : "按时结束" },
    { key: "truncated", label: "输出未截断", ok: result?.truncated === false, detail: result?.truncated ? "输出被截断" : "完整" },
    { key: "ok", label: "输出以 OK 结束", ok: lastLine === "OK", detail: lastLine ? `末行 “${lastLine.slice(0, 40)}”` : "无输出" },
  ];
}

/** @param {any} result */
export function isCheckResult(result) {
  return Boolean(result) && typeof result === "object" && "exit_code" in result && "output" in result;
}

/**
 * Did the fixed unittest check finish with a coherent result? This is distinct from
 * passing: a complete FAILED result can reject a patch, while missing or partial
 * execution cannot establish either patch success or patch failure.
 * @param {any} result
 * @returns {{complete: boolean, reason: string | null, testsRun: number, lastLine: string}}
 */
export function verificationEvidence(result) {
  const output = typeof result?.output === "string" ? result.output.replace(/\r\n?/g, "\n") : "";
  const lastLine = output.trimEnd().split("\n").at(-1) ?? "";
  const counts = [...output.matchAll(/^Ran (\d+) tests? in [^\n]+$/gm)];
  const parsedCount = counts.length ? Number(counts.at(-1)?.[1]) : 0;
  const testsRun = Number.isSafeInteger(parsedCount) && parsedCount > 0 ? parsedCount : 0;
  let reason = null;
  if (result?.timed_out !== false) reason = result?.timed_out === true
    ? "检查超时，可能只执行了一部分；不能判断补丁。"
    : "是否超时尚未确认，可能只执行了一部分；不能判断补丁。";
  else if (result?.truncated !== false) reason = result?.truncated === true
    ? "检查输出被截断，无法确认是否正常结束；不能判断补丁。"
    : "检查输出的完整性未确认；不能判断补丁。";
  else if (result?.exit_code !== 0 && result?.exit_code !== 1) reason = "检查进程没有按测试协议正常结束，可能未启动或只执行了一部分；不能判断补丁。";
  else if (testsRun === 0) reason = "没有确认实际运行的测试用例；不能判断补丁。";
  else if (result.tests_run != null && result.tests_run !== testsRun) reason = "用例统计与检查输出不一致，无法确认完整结果。";
  else if (lastLine !== "OK" && !/^FAILED(?: \([^\n]*\))?$/.test(lastLine)) reason = "缺少正常的测试结束记录，可能只执行了一部分；不能判断补丁。";
  else if ((lastLine === "OK") !== (result.exit_code === 0)) reason = "退出码与测试结束记录不一致，无法确认完整结果。";
  return { complete: reason === null, reason, testsRun, lastLine };
}
