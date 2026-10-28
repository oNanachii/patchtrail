#!/usr/bin/env node
/*
 * Browser acceptance for the built frontend (web/), driven over the Chrome DevTools Protocol
 * with no npm dependencies. Every scenario starts scripts/design_harness.py: the real FastAPI
 * app and RunService with a labelled fake runner (no container, no code executed, no model key).
 * One scenario starts the unmodified API (`create_app`) to record the real no-container state.
 *
 * Results are interface evidence only. They are not container, model or user acceptance.
 *
 *   node scripts/acceptance.mjs [--out ../artifacts/browser/frontend] [--chrome PATH] [--only a,b]
 */
import { spawn } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
const args = Object.fromEntries(process.argv.slice(2).reduce((pairs, value, index, all) =>
  value.startsWith("--") ? [...pairs, [value.slice(2), all[index + 1]]] : pairs, []));
const OUT = resolve(args.out ?? join(ROOT, "artifacts/browser/frontend"));
const CHROME = args.chrome ?? "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const PYTHON = join(ROOT, ".venv/bin/python");
const ONLY = args.only ? new Set(args.only.split(",")) : null;
const TOKEN = "acceptance-local-operator-token";
const DESKTOP = { width: 1440, height: 900, deviceScaleFactor: 1, mobile: false };
const MOBILE = { width: 390, height: 844, deviceScaleFactor: 2, mobile: true };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/* ---------- processes ---------- */
let port = 8890;
async function startServer({ harness = true, flags = [], dataDir, port: fixed }) {
  const p = fixed ?? port++;
  const argv = harness
    ? [join(ROOT, "scripts/design_harness.py"), "--port", String(p), "--data-dir", dataDir, "--operator-token", TOKEN, ...flags]
    : ["-m", "uvicorn", "patchtrail.api:create_app", "--factory", "--host", "127.0.0.1", "--port", String(p), "--workers", "1", "--no-access-log"];
  const env = { ...process.env, PATCHTRAIL_DATA_DIR: dataDir, PATCHTRAIL_ADMIN_TOKEN: TOKEN, DEEPSEEK_API_KEY: "" };
  const child = spawn(PYTHON, argv, { cwd: ROOT, env, stdio: ["ignore", "pipe", "pipe"] });
  let log = "";
  child.stdout.on("data", (d) => (log += d));
  child.stderr.on("data", (d) => (log += d));
  const base = `http://127.0.0.1:${p}`;
  for (let i = 0; i < 80; i++) {
    try {
      if ((await fetch(`${base}/api/health`)).ok) return { base, child, log: () => log, port: p, dataDir, flags, harness };
    } catch {}
    await sleep(150);
  }
  child.kill("SIGKILL");
  throw new Error(`server did not start: ${log}`);
}
const stop = async (server, signal = "SIGTERM") => {
  server.child.kill(signal);
  await new Promise((r) => server.child.once("exit", r));
};

async function launchChrome() {
  const profile = mkdtempSync(join(tmpdir(), "patchtrail-chrome-"));
  const chrome = spawn(CHROME, ["--headless=new", "--remote-debugging-port=0", `--user-data-dir=${profile}`,
    "--no-first-run", "--no-default-browser-check", "--disable-extensions", "--lang=zh-CN", "about:blank"],
  { stdio: ["ignore", "ignore", "pipe"] });
  const ws = await new Promise((resolveWs, reject) => {
    let buffer = "";
    chrome.stderr.on("data", (d) => {
      buffer += d;
      const match = /DevTools listening on (ws:\/\/\S+)/.exec(buffer);
      if (match) resolveWs(match[1]);
    });
    setTimeout(() => reject(new Error("Chrome did not start")), 15000);
  });
  return { chrome, ws, profile };
}

/* ---------- minimal CDP client ---------- */
class CDP {
  constructor(url) {
    this.id = 0;
    this.pending = new Map();
    this.listeners = [];
    this.socket = new WebSocket(url);
    this.ready = new Promise((r) => this.socket.addEventListener("open", r));
    this.socket.addEventListener("message", (event) => {
      const msg = JSON.parse(event.data);
      if (msg.id && this.pending.has(msg.id)) {
        const { resolve: ok, reject } = this.pending.get(msg.id);
        this.pending.delete(msg.id);
        msg.error ? reject(new Error(`${msg.error.message}`)) : ok(msg.result);
      } else if (msg.method) for (const listener of this.listeners) listener(msg);
    });
  }
  send(method, params = {}, sessionId) {
    const id = ++this.id;
    this.socket.send(JSON.stringify({ id, method, params, sessionId }));
    return new Promise((ok, reject) => this.pending.set(id, { resolve: ok, reject }));
  }
  on(listener) { this.listeners.push(listener); }
}

class Page {
  static async open(cdp, viewport) {
    const { targetId } = await cdp.send("Target.createTarget", { url: "about:blank" });
    const { sessionId } = await cdp.send("Target.attachToTarget", { targetId, flatten: true });
    const page = new Page(cdp, sessionId, targetId);
    cdp.on((msg) => msg.sessionId === sessionId && page.event(msg));
    for (const domain of ["Page", "Runtime", "Log"]) await page.send(`${domain}.enable`);
    await page.viewport(viewport);
    return page;
  }
  constructor(cdp, sessionId, targetId) {
    Object.assign(this, { cdp, sessionId, targetId, problems: [], intercept: null, requests: [] });
  }
  send(method, params) { return this.cdp.send(method, params, this.sessionId); }
  event(msg) {
    const p = msg.params;
    if (msg.method === "Runtime.exceptionThrown") this.problems.push(`exception: ${p.exceptionDetails.exception?.description ?? p.exceptionDetails.text}`);
    if (msg.method === "Runtime.consoleAPICalled" && ["error", "warning"].includes(p.type)) this.problems.push(`console.${p.type}: ${p.args.map((a) => a.value ?? a.description).join(" ")}`);
    if (msg.method === "Log.entryAdded" && ["error", "warning"].includes(p.entry.level)) {
      // Expected network failures in offline/lost-response scenarios are reported separately.
      if (!/Failed to load resource|net::ERR_/.test(p.entry.text)) this.problems.push(`log.${p.entry.level}: ${p.entry.text.slice(0, 300)}`);
    }
    if (msg.method === "Fetch.requestPaused") this.intercept?.(p);
  }
  async viewport(v) {
    this.size = v;
    await this.send("Emulation.setDeviceMetricsOverride", v);
    await this.send("Emulation.setTouchEmulationEnabled", { enabled: v.mobile });
  }
  async scheme(value) {
    await this.send("Emulation.setEmulatedMedia", { features: [{ name: "prefers-color-scheme", value }] });
  }
  async goto(url) {
    await this.send("Page.navigate", { url });
    await this.waitFor("document.readyState === 'complete' && document.querySelector('.topbar')", 10000);
    await sleep(400);
  }
  async eval(expression) {
    const { result, exceptionDetails } = await this.send("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true });
    if (exceptionDetails) throw new Error(`eval failed: ${exceptionDetails.exception?.description ?? exceptionDetails.text}`);
    return result.value;
  }
  async waitFor(expression, timeout = 15000) {
    const end = Date.now() + timeout;
    while (Date.now() < end) {
      try { if (await this.eval(`!!(${expression})`)) return; } catch {}
      await sleep(150);
    }
    throw new Error(`timed out waiting for ${expression}`);
  }
  waitText(text, timeout) { return this.waitFor(`document.body.innerText.includes(${JSON.stringify(text)})`, timeout); }
  /** Real mouse click on the first visible element matching selector and text. */
  async click(selector, text = "") {
    const box = await this.eval(`(() => {
      const el = [...document.querySelectorAll(${JSON.stringify(selector)})].find((e) => e.offsetParent !== null && e.textContent.includes(${JSON.stringify(text)}));
      if (!el) return null;
      el.scrollIntoView({ block: "center" });
      const r = el.getBoundingClientRect();
      return { x: r.left + r.width / 2, y: r.top + r.height / 2 };
    })()`);
    if (!box) throw new Error(`no element ${selector} "${text}"`);
    await sleep(120);
    for (const type of ["mouseMoved", "mousePressed", "mouseReleased"]) {
      await this.send("Input.dispatchMouseEvent", { type, x: box.x, y: box.y, button: "left", clickCount: 1 });
    }
    await sleep(200);
  }
  async key(name, times = 1) {
    const keys = {
      Tab: { key: "Tab", code: "Tab", windowsVirtualKeyCode: 9 },
      Enter: { key: "Enter", code: "Enter", windowsVirtualKeyCode: 13, text: "\r" },
      Escape: { key: "Escape", code: "Escape", windowsVirtualKeyCode: 27 },
      Space: { key: " ", code: "Space", windowsVirtualKeyCode: 32, text: " " },
      ArrowDown: { key: "ArrowDown", code: "ArrowDown", windowsVirtualKeyCode: 40 },
    };
    for (let i = 0; i < times; i++) {
      await this.send("Input.dispatchKeyEvent", { type: "keyDown", ...keys[name] });
      await this.send("Input.dispatchKeyEvent", { type: "keyUp", ...keys[name], text: undefined });
      await sleep(60);
    }
    await sleep(150);
  }
  async type(text) {
    await this.send("Input.insertText", { text });
  }
  focused() {
    return this.eval(`(() => { const e = document.activeElement; return e ? (e.tagName + " " + (e.getAttribute("aria-label") || e.textContent || "").trim().replace(/\\s+/g, " ")).slice(0, 70) : null; })()`);
  }
  async shot(name, { full = true } = {}) {
    if (full) await this.eval("window.scrollTo(0, 0); document.querySelector('.sidebar')?.scrollTo(0, 0)");
    await sleep(350);
    const overflow = await this.eval("Math.max(0, document.documentElement.scrollWidth - document.documentElement.clientWidth)");
    let clip;
    if (full) {
      const { cssContentSize } = await this.send("Page.getLayoutMetrics");
      clip = { x: 0, y: 0, width: this.size.width, height: Math.min(Math.ceil(cssContentSize.height), 9000), scale: 1 };
    }
    const { data } = await this.send("Page.captureScreenshot", { format: "png", captureBeyondViewport: full, ...(clip ? { clip } : {}) });
    writeFileSync(join(OUT, `${name}.png`), Buffer.from(data, "base64"));
    return { name: `${name}.png`, horizontalOverflowPx: overflow };
  }
  async close() { await this.cdp.send("Target.closeTarget", { targetId: this.targetId }); }
}

/* ---------- helpers on top of the page ---------- */
const badge = (page) => page.eval("document.querySelector('.run-head .badge')?.textContent ?? null");
async function waitBadge(page, label, timeout = 20000) {
  await page.waitFor(`document.querySelector('.run-head .badge')?.textContent === ${JSON.stringify(label)}`, timeout);
}
async function focusButton(page, text) {
  await page.eval(`[...document.querySelectorAll("button")].find((b) => b.offsetParent !== null && b.textContent.includes(${JSON.stringify(text)}))?.focus()`);
  return page.focused();
}
async function apiRuns(server) {
  return (await (await fetch(`${server.base}/api/runs`, { headers: { Authorization: `Bearer ${TOKEN}` } })).json()).runs;
}

/* ---------- scenarios ---------- */
const scenarios = {
  async overview({ cdp, tmp }) {
    const server = await startServer({ dataDir: join(tmp, "overview") });
    const page = await Page.open(cdp, DESKTOP);
    const shots = [];
    try {
      await page.scheme("light");
      await page.goto(server.base + "/");
      await page.waitText("库存数量缺口");
      shots.push(await page.shot("01-overview-desktop-light"));
      await page.scheme("dark");
      shots.push(await page.shot("02-overview-desktop-dark"));
      await page.scheme("light");
      await page.viewport(MOBILE);
      await page.goto(server.base + "/");
      await page.waitText("库存数量缺口");
      shots.push(await page.shot("03-overview-mobile"));
      // Keyboard order from the top of the page.
      await page.eval("document.activeElement?.blur(); window.scrollTo(0, 0)");
      const order = [];
      for (let i = 0; i < 9; i++) { await page.key("Tab"); order.push(await page.focused()); }
      // Arrow keys move inside the native task radio group.
      await page.eval(`document.querySelector('input[name="task"]').focus()`);
      await page.key("ArrowDown");
      const arrow = await page.eval(`document.querySelector('input[name="task"]:checked').value`);
      return { shots, keyboard: { tabOrderMobile: order, arrowDownSelectsTask: arrow }, problems: page.problems };
    } finally { await page.close(); await stop(server); }
  },

  async "demo-success"({ cdp, tmp }) {
    const server = await startServer({ dataDir: join(tmp, "success") });
    const page = await Page.open(cdp, DESKTOP);
    const shots = [];
    const keyboard = {};
    try {
      await page.scheme("light");
      await page.goto(server.base + "/");
      await page.waitText("库存数量缺口");
      await page.click("button", "开始脚本演示");
      await waitBadge(page, "等待确认");
      shots.push(await page.shot("04-awaiting-approval-desktop"));
      keyboard.openerFocused = await focusButton(page, "确认并验证");
      await page.key("Enter");
      await page.waitFor("document.querySelector('dialog[open]')");
      keyboard.dialogInitialFocus = await page.focused();
      shots.push(await page.shot("05-approve-dialog-desktop", { full: false }));
      await page.key("Escape");
      keyboard.escapeClosesDialog = await page.eval("!document.querySelector('dialog[open]')");
      keyboard.focusAfterEscape = await page.focused();
      await page.key("Enter");
      await page.waitFor("document.querySelector('dialog[open]')");
      await page.key("Tab");
      keyboard.confirmButton = await page.focused();
      await page.key("Enter");
      await waitBadge(page, "验证通过");
      keyboard.focusAfterApprove = await page.focused();
      await page.eval("document.querySelectorAll('.tool').forEach((d, i) => { if (i === 1) d.open = true; })");
      shots.push(await page.shot("06-succeeded-desktop"));
      await page.scheme("dark");
      shots.push(await page.shot("07-succeeded-desktop-dark"));
      await page.scheme("light");
      const runs = await apiRuns(server);
      // Report download: the saved file must be the server's report for this run.
      const downloads = join(tmp, "downloads");
      mkdirSync(downloads, { recursive: true });
      await cdp.send("Browser.setDownloadBehavior", { behavior: "allow", downloadPath: downloads });
      await page.click("button", "下载报告");
      const file = join(downloads, `patchtrail-report-${runs[0].id}.json`);
      for (let i = 0; i < 50 && !existsSync(file); i++) await sleep(100);
      const report = JSON.parse(readFileSync(file, "utf8"));
      const download = { file: readdirSync(downloads), runId: report.run.id === runs[0].id, state: report.run.state,
        events: report.events.length, evidenceBoundary: Boolean(report.evidence_boundary) };
      return { shots, keyboard, download, serverStates: runs.map((r) => r.state), problems: page.problems };
    } finally { await page.close(); await stop(server); }
  },

  async "mobile-flow"({ cdp, tmp }) {
    const server = await startServer({ dataDir: join(tmp, "mobile") });
    const page = await Page.open(cdp, MOBILE);
    const shots = [];
    try {
      await page.scheme("light");
      await page.goto(server.base + "/");
      await page.waitText("库存数量缺口");
      await page.click("label.task-option", "补货批次到期");
      await page.click("button", "开始脚本演示");
      await waitBadge(page, "等待确认");
      const headingFocused = await page.focused();
      shots.push(await page.shot("08-awaiting-mobile"));
      await page.click("button", "确认并验证");
      await page.waitFor("document.querySelector('dialog[open]')");
      shots.push(await page.shot("09-approve-dialog-mobile", { full: false }));
      await page.click("dialog button", "确认 ");
      await waitBadge(page, "验证通过");
      shots.push(await page.shot("10-succeeded-mobile"));
      await page.click("button", "全部运行");
      await page.waitText("最近运行");
      shots.push(await page.shot("11-list-mobile"));
      return { shots, focusAfterStartOnMobile: headingFocused, problems: page.problems };
    } finally { await page.close(); await stop(server); }
  },

  async "lost-create"({ cdp, tmp }) {
    const server = await startServer({ dataDir: join(tmp, "lost") });
    const page = await Page.open(cdp, DESKTOP);
    const shots = [];
    const seen = [];
    try {
      await page.scheme("light");
      await page.goto(server.base + "/");
      await page.waitText("库存数量缺口");
      // Let the POST reach the server and commit, then drop the response before the page sees it.
      await page.send("Fetch.enable", { patterns: [{ urlPattern: "*/api/runs", requestStage: "Response" }] });
      page.intercept = async (p) => {
        if (p.request.method === "POST") {
          seen.push({ key: p.request.headers["Idempotency-Key"], status: p.responseStatusCode, dropped: true });
          await page.send("Fetch.failRequest", { requestId: p.requestId, errorReason: "ConnectionReset" });
        } else await page.send("Fetch.continueRequest", { requestId: p.requestId });
      };
      await page.click("label.task-option", "标签与历史原子性");
      await page.click("button", "开始脚本演示");
      await page.waitText("请求结果未知");
      shots.push(await page.shot("12-lost-create-response"));
      const stored = await page.eval("sessionStorage.getItem('patchtrail.pendingStart')");
      await page.send("Fetch.disable");
      page.intercept = null;
      await page.goto(server.base + "/");
      await page.waitText("核对上次创建请求");
      shots.push(await page.shot("13-pending-after-reload"));
      const runsBefore = await apiRuns(server);
      await page.send("Fetch.enable", { patterns: [{ urlPattern: "*/api/runs", requestStage: "Request" }] });
      page.intercept = async (p) => {
        if (p.request.method === "POST") seen.push({ key: p.request.headers["Idempotency-Key"], body: p.request.postData });
        await page.send("Fetch.continueRequest", { requestId: p.requestId });
      };
      await page.click("button", "核对上次创建请求");
      await page.waitFor("document.querySelector('.run-head')");
      await page.send("Fetch.disable");
      const runsAfter = await apiRuns(server);
      return {
        shots,
        pendingStored: JSON.parse(stored),
        tokenInSessionStorage: Boolean(stored && stored.includes(TOKEN)),
        requests: seen,
        sameKey: seen.length === 2 && seen[0].key === seen[1].key,
        serverRunsBeforeRetry: runsBefore.length,
        serverRunsAfterRetry: runsAfter.length,
        pendingCleared: (await page.eval("sessionStorage.getItem('patchtrail.pendingStart')")) === null,
        problems: page.problems,
      };
    } finally { await page.close(); await stop(server); }
  },

  async "verify-fail"({ cdp, tmp }) {
    const server = await startServer({ dataDir: join(tmp, "vfail"), flags: ["--verify", "fail"] });
    const page = await Page.open(cdp, DESKTOP);
    try {
      await page.scheme("light");
      await page.goto(server.base + "/");
      await page.waitText("库存数量缺口");
      await page.click("button", "开始脚本演示");
      await waitBadge(page, "等待确认");
      await page.click("button", "确认并验证");
      await page.click("dialog button", "确认 ");
      await waitBadge(page, "验证未通过");
      const shots = [await page.shot("14-verifier-failed-desktop")];
      return { shots, stopReason: (await apiRuns(server))[0].stop_reason, problems: page.problems };
    } finally { await page.close(); await stop(server); }
  },

  async "not-executed"({ cdp, tmp }) {
    const server = await startServer({ dataDir: join(tmp, "noexec"), flags: ["--verify", "unavailable"] });
    const page = await Page.open(cdp, DESKTOP);
    try {
      await page.scheme("light");
      await page.goto(server.base + "/");
      await page.waitText("库存数量缺口");
      await page.click("button", "开始脚本演示");
      await waitBadge(page, "等待确认");
      await page.click("button", "确认并验证");
      await page.click("dialog button", "确认 ");
      await waitBadge(page, "容器不可用");
      const shots = [await page.shot("15-container-lost-during-verify")];
      const verdict = await page.eval("document.querySelector('.verdict-title')?.textContent");
      return { shots, verdictTitle: verdict, stopReason: (await apiRuns(server))[0].stop_reason, problems: page.problems };
    } finally { await page.close(); await stop(server); }
  },

  async interrupted({ cdp, tmp }) {
    const dataDir = join(tmp, "interrupt");
    let server = await startServer({ dataDir, flags: ["--provider-seconds", "8"] });
    const page = await Page.open(cdp, DESKTOP);
    const shots = [];
    try {
      await page.scheme("light");
      await page.goto(server.base + "/");
      await page.waitText("库存数量缺口");
      await page.click("button", "开始脚本演示");
      await waitBadge(page, "调查中");
      await sleep(1500); // inside the first (slow) scripted request
      await stop(server, "SIGKILL");
      await page.waitText("无法连接 PatchTrail 服务", 15000);
      shots.push(await page.shot("16-service-unreachable"));
      // Same port: the open page reconnects on its own and shows the recovered state.
      server = await startServer({ dataDir, flags: [], port: server.port });
      await waitBadge(page, "已中断", 20000);
      const offlineBannerCleared = await page.eval("!document.querySelector('.banner-offline')");
      shots.push(await page.shot("17-interrupted-desktop"));
      await focusButton(page, "明确恢复");
      await page.key("Enter");
      await page.waitFor("document.querySelector('dialog[open]')");
      const dialogFocus = await page.focused();
      shots.push(await page.shot("18-resume-dialog", { full: false }));
      await page.key("Tab");
      await page.key("Enter");
      await waitBadge(page, "等待确认", 25000);
      await page.eval("[...document.querySelectorAll('.trail-title')].find((e) => e.textContent.includes('明确恢复'))?.scrollIntoView({ block: 'center' })");
      shots.push(await page.shot("19-resumed-awaiting"));
      const trail = await page.eval("[...document.querySelectorAll('.trail-title')].map((e) => e.textContent.trim().replace(/\\s+/g, ' '))");
      return { shots, offlineBannerCleared, dialogInitialFocus: dialogFocus, trailTitles: trail, problems: page.problems };
    } finally { await page.close(); await stop(server); }
  },

  async cancel({ cdp, tmp }) {
    const server = await startServer({ dataDir: join(tmp, "cancel"), flags: ["--provider-seconds", "3"] });
    const page = await Page.open(cdp, DESKTOP);
    try {
      await page.scheme("light");
      await page.goto(server.base + "/");
      await page.waitText("库存数量缺口");
      await page.click("button", "开始脚本演示");
      await waitBadge(page, "调查中");
      await page.click("button", "取消运行…");
      await page.waitFor("document.querySelector('dialog[open]')");
      const initial = await page.focused();
      await page.click("dialog button.button-danger", "取消运行");
      await waitBadge(page, "已取消");
      await sleep(3500); // the in-flight scripted request finishes; its result must not reopen the run
      const shots = [await page.shot("20-cancelled")];
      return { shots, dialogInitialFocus: initial, finalBadge: await badge(page), serverState: (await apiRuns(server))[0].state, problems: page.problems };
    } finally { await page.close(); await stop(server); }
  },

  async "operator-and-live"({ cdp, tmp }) {
    const server = await startServer({ dataDir: join(tmp, "live") });
    const page = await Page.open(cdp, DESKTOP);
    const shots = [];
    try {
      await page.scheme("light");
      await page.goto(server.base + "/");
      await page.waitText("库存数量缺口");
      await page.click("label.mode-option", "DeepSeek");
      const anonymous = await page.eval("[...document.querySelectorAll('.blockers li')].map((e) => e.textContent.trim())");
      const startDisabled = await page.eval("[...document.querySelectorAll('button')].find((b) => b.textContent.includes('开始真实调用')).disabled");
      shots.push(await page.shot("21-live-needs-operator", { full: false }));
      await page.click("button.operator-button");
      await page.waitFor("document.querySelector('dialog[open] input[type=password]')");
      const focus = await page.focused();
      await page.type(TOKEN);
      shots.push(await page.shot("22-operator-dialog", { full: false }));
      await page.key("Enter");
      await page.waitFor("!document.querySelector('dialog[open]')");
      const withToken = await page.eval("[...document.querySelectorAll('.blockers li')].map((e) => e.textContent.trim())");
      shots.push(await page.shot("23-live-model-not-configured", { full: false }));
      const storage = await page.eval(`JSON.stringify({ local: { ...localStorage }, session: { ...sessionStorage }, cookie: document.cookie, url: location.href })`);
      return {
        shots, anonymousBlockers: anonymous, liveStartDisabled: startDisabled, operatorDialogFocus: focus,
        blockersWithToken: withToken, tokenPersisted: storage.includes(TOKEN), problems: page.problems,
      };
    } finally { await page.close(); await stop(server); }
  },

  async "private-and-offline-runner"({ cdp, tmp }) {
    const server = await startServer({ dataDir: join(tmp, "private"), flags: ["--private", "--runner", "offline"] });
    const page = await Page.open(cdp, DESKTOP);
    const shots = [];
    try {
      await page.scheme("light");
      await page.goto(server.base + "/");
      await page.waitText("查看运行需要操作令牌");
      shots.push(await page.shot("24-private-demo-and-no-container", { full: false }));
      const blockers = await page.eval("[...document.querySelectorAll('.blockers li')].map((e) => e.textContent.trim())");
      return { shots, blockers, problems: page.problems };
    } finally { await page.close(); await stop(server); }
  },

  async "real-api-no-container"({ cdp, tmp }) {
    // Unmodified create_app with the real DockerRunner; this Mac has no container runtime.
    const server = await startServer({ harness: false, dataDir: join(tmp, "real") });
    const page = await Page.open(cdp, DESKTOP);
    try {
      await page.scheme("light");
      await page.goto(server.base + "/");
      await page.waitText("库存数量缺口");
      const shots = [await page.shot("25-real-api-no-container", { full: false })];
      const health = await (await fetch(`${server.base}/api/health`)).json();
      const create = await fetch(`${server.base}/api/runs`, { method: "POST", headers: { "Content-Type": "application/json", "Idempotency-Key": "acceptance-real-0001" }, body: JSON.stringify({ task_id: "quantity-shortfall", mode: "demo" }) });
      return {
        shots, harnessBanner: await page.eval("!!document.querySelector('.banner-harness')"),
        containerAvailable: health.container_available, createStatus: create.status, createBody: await create.json(),
        blockers: await page.eval("[...document.querySelectorAll('.blockers li')].map((e) => e.textContent.trim())"),
        problems: page.problems,
      };
    } finally { await page.close(); await stop(server); }
  },
};

/* ---------- main ---------- */
mkdirSync(OUT, { recursive: true });
const tmp = mkdtempSync(join(tmpdir(), "patchtrail-acceptance-"));
const { chrome, ws, profile } = await launchChrome();
const cdp = new CDP(ws);
await cdp.ready;
const results = { date: new Date().toISOString(), scope: "headless Chrome against web/ build; design harness uses a fake runner (no container, no model); not container, model or user acceptance", scenarios: {} };
for (const [name, run] of Object.entries(scenarios)) {
  if (ONLY && !ONLY.has(name)) continue;
  const started = Date.now();
  try {
    results.scenarios[name] = { status: "passed", ms: 0, ...(await run({ cdp, tmp })) };
  } catch (error) {
    results.scenarios[name] = { status: "failed", error: String(error?.stack ?? error) };
  }
  results.scenarios[name].ms = Date.now() - started;
  console.log(`${results.scenarios[name].status.padEnd(7)} ${name} (${results.scenarios[name].ms} ms)`);
}
// A partial run (--only) never overwrites the full record.
writeFileSync(join(OUT, ONLY ? "results-partial.json" : "results.json"), JSON.stringify(results, null, 2));
chrome.kill();
await new Promise((r) => chrome.once("exit", r));
rmSync(profile, { recursive: true, force: true, maxRetries: 5, retryDelay: 200 });
rmSync(tmp, { recursive: true, force: true, maxRetries: 5, retryDelay: 200 });
process.exit(Object.values(results.scenarios).some((s) => s.status === "failed") ? 1 : 0);
