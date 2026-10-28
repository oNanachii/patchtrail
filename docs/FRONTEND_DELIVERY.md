# PatchTrail 前端交付说明

2026-10-28，Claude Code（用户本人操作）完成。供 Codex 独立核对与部署。本文只陈述实际执行过的检查；未验收部分单独列出。

## 结论速览

- `web/` 已替换为新的构建产物（React 19 + TypeScript + Vite 8），源码在 `frontend/`。后端 Agent 运行时、API、审批、鉴权和容器边界均未修改。
- 新增仅供开发的 `scripts/design_harness.py`：真实 `create_app` + 真实 `RunService`，只把容器 runner 换成**明确标记的假 runner**（不启动容器、不执行任何代码、live 无模型密钥）。它在 `/api/health` 额外返回 `design_harness` 字段，前端据此显示“设计测试环境，不是证据”的横幅；生产后端从不返回该字段。
- 浏览器验收：无头 Chrome 跑 11 个场景（桌面 1440×900 与 390×844），全部通过，0 个控制台/CSP 错误，0 处横向溢出；另在桌面应用内置浏览器中做了键盘与 390px 目视检查。截图与结果在 `artifacts/browser/frontend/`。
- 单元/契约：`frontend` 12 项 node:test 通过；Python 全套 76 项通过（含其他会话并行加入的后端测试）。
- **未验收**：真实容器验证、真实 DeepSeek live 运行在新界面中的呈现、Safari/Firefox、读屏软件、真机触屏、用户验收。详见文末。

## 构建与运行

需要 Node ≥ 20.19（本机 v24.21.0）。服务器部署**不需要 Node**：构建产物已在 `web/`。

```sh
cd frontend
npm ci                 # 依赖版本精确锁定在 package-lock.json
npm test               # 纯 JS 逻辑测试（node:test，无额外依赖）
npm run build          # tsc --noEmit 后 vite build，清空并重写 ../web
```

- 产物结构：`web/index.html`，以及 `web/index-<hash>.js|css`、`web/*.woff2`、`web/favicon.svg`。HTML 以 `/assets/<file>` 引用它们，正好对应 `api.py` 的 `app.mount("/assets", StaticFiles(directory=web))` 与 `GET /` → `web/index.html`。`scripts/serve_frontend.py` 的 `/assets/*` 映射同样适用，无需修改。
- 与 API 的 CSP（`default-src 'self'; script-src 'self'; style-src 'self'`）兼容：无内联脚本/样式、无 `style=` 属性、无外部字体或 CDN；`assetsInlineLimit: 0` 防止字体被内联成 `data:`（首次构建时实际触发过此 CSP 拦截，已修复）。`tests/test_frontend.py` 静态检查这些约束。
- 开发：`npm run dev` 在 `127.0.0.1:5173` 启动 Vite，并把 `/api` 代理到 `PATCHTRAIL_API`（默认 `http://127.0.0.1:8874`）。开发服务器不施加 CSP，最终检查须用构建产物经 FastAPI 提供。

设计测试环境（仅回环，不读取 `.env`，不会调用模型）：

```sh
.venv/bin/python scripts/design_harness.py --port 8875 --operator-token <本地测试值>
# 失败路径：--verify fail | --verify unavailable | --runner offline | --private
# 中断：--provider-seconds 8 后强制结束进程，再用同一 --data-dir 重启
```

浏览器验收（自动启动/停止 harness 与一次未修改的真实 API；需要本机 Google Chrome）：

```sh
cd frontend && node scripts/acceptance.mjs   # --only a,b 只跑部分场景，写入 results-partial.json
```

## 界面结构与设计取舍

- **布局**：左侧“新建运行 + 最近运行”，右侧运行详情。详情顺序：模式横幅 → 五段阶段轨迹（排队 / 调查 / 人工确认 / 容器验证 / 结论）→ “下一步”提示 → 补丁审阅与检查结果（主列）→ 预算、任务与执行记录（侧列）。主区宽度 ≥1000px 时双列（容器查询），否则单列。<960px 视口为列表/详情两屏切换，带“全部运行”返回；所选运行写在 URL hash（`#run=<id>`），支持前进后退与深链接。
- **配色原则**：中性色承载界面，色彩只表达语义——运行状态、diff 增删、演示/真实模式。亮暗两套主题，跟随系统并可手动切换（仅主题偏好存 localStorage）。中文用系统字体，代码/哈希用自托管 JetBrains Mono。
- **演示与真实调用的区分**：脚本演示用虚线斜纹标签与横幅，并写明“没有调用模型；补丁是预置修复，不代表模型能力”；DeepSeek 用实心紫色标签，横幅显示配置模型、API 返回模型（`observed_models`）与 Token。每个执行轮次标注“未调用模型 / 真实调用”。
- **结论只来自验证器**：检查结果卡片逐条列出与 `CheckResult.passed` 相同的判定条件（exit 0、实际运行用例数 > 0、未超时、未截断、末行 OK），并给出基线（修复前）与验证（修复后）的逐用例对照。执行器名不是 `docker`/`podman` 时显示“非容器执行器 · 结果不是证据”。模型文本（`model_response.content`、补丁说明）一律标注“未经验证，不是结果”。
- **未执行 ≠ 失败**：`sandbox_unavailable`、`baseline_unconfirmed`、`verification_contract` 用警告色并写明“补丁未被执行”，不显示为补丁失败或成功。
- **没有数据就显示真实空态**：无运行、未提出补丁、尚无验证输出、匿名只见演示运行、私有模式需令牌等，都按 API 实际返回呈现；没有示例数据、虚构指标或占位按钮。

## 必须保留的规则如何实现

| 规则 | 实现位置 | 证据 |
| --- | --- | --- |
| 操作令牌只在页面内存 | `App.tsx` React state；只经 `Authorization` 头发送 | `operator-and-live` 场景检查 localStorage/sessionStorage/Cookie/URL 均不含令牌；`test_frontend.py` 静态检查 |
| 创建幂等与重试 | `lib/createGuard.js`：发送前把 `{key, body}`（不含令牌）写入 sessionStorage；只有拿到运行或收到“证明不存在运行”的响应才清除 | node 测试 6 项；`lost-create` 场景：真实服务端已创建、浏览器丢弃响应→刷新→同一键重试，服务端仍只有 1 个运行 |
| 失败分类 | 401/`operator_not_configured`→保留并请求令牌（鉴权早于幂等查找）；409→键冲突；`sandbox_unavailable`/`model_not_configured`/`queue_full`/`rate_limited` 及其余 4xx→确认未创建；网络错误、5xx、`storage_busy`、无法确认的 2xx→保留同键 | node 测试 |
| live 鉴权 | live 模式在缺令牌/缺密钥/未配置操作令牌时禁用开始并说明原因；开始前有付费确认弹窗；401 详情页显示令牌入口 | `operator-and-live`、`private-and-offline-runner` 场景 |
| 补丁按 digest 与版本确认 | 打开确认时快照 `{runId, digest, version}`，只发送快照值；运行在审阅期间变化则禁用确认并提示重新审阅；网络/5xx 结果未知时允许用同一确认重试（服务端幂等）；409 提示重新审阅 | `demo-success`、`mobile-flow` 场景 |
| 明确恢复 | 中断运行显示恢复按钮与弹窗，携带打开时的 `expected_version`；`unconfirmed_model_request` 时警告会发起新请求（live 可能产生费用） | `interrupted` 场景：强制结束进程→页面自动显示失联→同端口重启后自动显示“已中断”→恢复→执行记录出现“明确恢复：上一次请求结果未知” |
| 报告下载 | `GET /api/runs/{id}/report` 存为 `patchtrail-report-<id>.json` | `demo-success` 场景：无头 Chrome 实际下载，文件解析为该运行的报告（状态 succeeded、26 条事件、含 evidence_boundary） |
| 增量事件 | 先取运行、后取 `events?after=<cursor>`，进行中 1.5s 轮询，终态停止，页面隐藏时暂停 | 各场景 |

## 实际验收结果

无头 Chrome（系统 Google Chrome，CDP 驱动），结果见 `artifacts/browser/frontend/results.json`。除最后一个场景外均使用设计 harness 的假 runner，**这些是界面行为证据，不是容器或模型证据**。

| 场景 | 内容 | 结果 |
| --- | --- | --- |
| overview | 空态，桌面亮/暗、390px；Tab 顺序；方向键切换任务单选 | 通过 |
| demo-success | 开始演示→等待确认→键盘打开确认（初始焦点在“返回审阅”）→Esc 关闭并回到触发按钮→Tab+Enter 确认→验证通过；确认后焦点移到运行标题；下载报告 | 通过，服务端状态 `succeeded`，报告文件内容正确 |
| mobile-flow | 390px 选择任务→开始→焦点到详情标题→确认→通过→返回列表 | 通过 |
| lost-create | 见上表 | 通过，同一键 2 次请求，服务端 1 个运行，记录已清除 |
| verify-fail | `--verify fail` | 通过，显示“验证未通过”，`verifier_failed` |
| not-executed | `--verify unavailable` | 通过，显示“容器不可用，代码未执行”，`sandbox_unavailable` |
| interrupted | 见上表 | 通过，失联横幅自动消失 |
| cancel | 调查中取消（初始焦点“保留运行”），进行中的脚本请求结束后仍为已取消 | 通过 |
| operator-and-live | 匿名时 live 被禁用；载入令牌后仅剩“未配置 DeepSeek 密钥” | 通过，令牌未持久化 |
| private-and-offline-runner | 关闭公开演示 + 无容器 | 通过 |
| real-api-no-container | **未修改的** `create_app` 与真实 DockerRunner（本机无 Docker/Podman） | 通过：无 harness 横幅，健康检查 `container_available=false`，开始被禁用；同一后端 `POST /api/runs` 实际返回 503 `sandbox_unavailable` |

截图（`artifacts/browser/frontend/`，约 9.6 MB，未提交到 Git，是否入库由 Codex 决定）：`01–03` 空态，`04–07` 桌面确认与成功（含暗色），`08–11` 390px 流程，`12–13` 丢失响应与刷新后核对，`14` 验证未通过，`15` 验证时容器不可用，`16–19` 失联、中断、恢复，`20` 已取消，`21–23` 令牌与 live 限制，`24` 私有模式与无容器，`25` 真实 API 无容器。

开发过程中由浏览器检查实际发现并已修复：字体 `data:` 内联被 CSP 拦截；React `autoFocus` 早于 `showModal()` 导致弹窗初始焦点落在关闭按钮；确认后触发按钮消失致焦点丢到 `body`；展开的测试输出撑出 906px 横向溢出；390px 下 diff 改动行被截断（窄屏默认自动换行）；刷新后“核对上次创建”提示在首屏之外；“18 分 60 秒”时长进位错误；列表状态滞后于详情。

## 未验收与已知限制

- **真实容器**：本机没有 Docker/Podman；未连接服务器（按要求不操作服务器，也未建立 SSH 转发）。新界面尚未展示过真实 `podman` 验证结果。部署前应按 `docs/CLAUDE_FRONTEND_HANDOFF.md` 的回环转发跑一次演示流程。
- **真实 DeepSeek**：未发起任何模型调用。live 运行详情（`model_response` 文本、`observed_models`、Token 明细、live 401/重新鉴权）只按代码与 API 形状实现，**没有用真实 live 运行数据在浏览器中查看过**。`artifacts/live-model-validation.json` 是摘要，不含完整事件，无法替代。
- **harness 与生产的差异**：`verification_started` 事件中的 `runner: "OCI container"` 是服务端常量，harness 下同样出现；实际执行器以检查结果中的 `runner` 为准（harness 显示 `design-harness-fake`）。确认弹窗描述的是生产容器行为，harness 下由顶部横幅说明不适用。
- **浏览器与辅助技术**：只测了 Chromium（无头 Chrome 与桌面应用内置浏览器）。未测 Safari/Firefox、未用 VoiceOver/NVDA 实测；无障碍仅基于原生控件、`<dialog>`、标签与焦点检查。390px 为设备模拟，未在真机触屏测试。
- **体积**：JS 约 301 KB（gzip 约 92 KB），React 占大部分；当前未做代码分割。
- **并行修改**：本轮期间另一会话修改并提交了后端（`model_response` 事件、`configured_model`/`observed_models`、`runtime_code_hash`、`storage_busy` 503）。前端兼容新旧两种响应；`README.md`、`PROJECT_STATUS.md` 和 `artifacts/validation.json` 正由该会话编辑，本次没有改动它们。
- 用户本人验收与 Codex 独立复核尚未进行。

## 文件清单

- `frontend/`：`src/lib/*.js`（可在 Node 中直接测试的纯逻辑：API 客户端、幂等创建、diff/unittest 解析、执行记录与阶段推导、文案）、`src/components/*.tsx`、`src/styles/*.css`、`tests/*.test.js`、`scripts/acceptance.mjs`。
- `web/`：构建产物（替换原功能基线 `app.js`/`style.css`）。
- `scripts/design_harness.py`：开发用设计测试环境。
- `tests/test_frontend.py`：运行 node 测试并静态检查构建产物；删除了针对旧 `web/app.js` 的 `tests/frontend_retry.cjs`（其三项检查已迁移到 `frontend/tests/createGuard.test.js`）。
- `.gitignore`：增加 `frontend/node_modules/`。
