# 通过 MCP 使用 PatchTrail

本地 stdio server 使用[官方 MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk)，固定版本为 `mcp==2.3.0`。它连接已经运行的本地 PatchTrail API，让 Claude Code、Codex 等调用方发起和观察**脚本演示**，不调用付费模型。

## 工具与审批边界

| 工具 | 参数 | 行为 |
| --- | --- | --- |
| `list_tasks` | 无 | 读取匿名 API 可见的内置公共任务 |
| `start_demo_run` | `task_id`、`request_key` | 创建脚本演示运行，固定 `mode=demo`；返回后用 `get_run` 查询进度 |
| `get_run` | `run_id`、可选 `after=0` | 读取状态、事件、补丁 diff、测试结果和网页审阅提示 |
| `list_runs` | 无 | 读取匿名 API 可见的最近演示运行 |

`request_key` 必须为 8–128 个 ASCII 字母、数字或 `.`、`_`、`:`、`-`。同一个任务重用相同键会复用已有运行；把同一键用于另一项请求会被拒绝。如果启动请求超时，先查看运行列表；需要重新查询创建结果时，保留原来的键和参数，避免重复创建。适配器不自动重试请求。

**没有批准补丁的 MCP 工具。批准必须由人在网页上完成，不能让调用方 Agent 自己批准自己发起的补丁。** 当状态为 `awaiting_approval` 时，人打开返回的审阅链接（形如 `http://127.0.0.1:8874/#run=<run_id>`），检查 diff，再确认准确的补丁 digest 和版本；也可以在同一个网页的运行列表中选择对应记录。批准后仍须实际容器测试通过，运行才能成功。

这限制的是 MCP 适配器的能力：既有公开演示 HTTP 审批接口保持原行为，省略 MCP 工具不等于能识别或阻止另外拥有 HTTP、浏览器能力的 Agent。不要把这一设计描述为通用的“证明操作者是人”。

## 本地准备

在 PatchTrail 目录安装可选依赖；该文件也包含主项目依赖：

```sh
.venv/bin/python -m pip install -r requirements-mcp.lock.txt
```

先按 [README](../README.md#在线演示和快速开始) 准备 Docker 或 Podman 和固定测试镜像。终端一启动本地 API；以下示例使用独立的演示数据目录，并将模型 Key 留空：

```sh
PATCHTRAIL_DATA_DIR="$PWD/var/mcp-demo" \
PATCHTRAIL_PUBLIC_DEMO=true \
DEEPSEEK_API_KEY='' PATCHTRAIL_ADMIN_TOKEN='' \
.venv/bin/python -m uvicorn patchtrail.api:create_app --factory \
  --host 127.0.0.1 --port 8874 --workers 1 --no-access-log
```

浏览器打开 [本地网页](http://127.0.0.1:8874/)。使用 Podman 时按 README 配置容器 CLI；容器不可用时，启动运行会报告不可用，不在宿主执行任务代码。

终端二可以直接启动 stdio server：

```sh
.venv/bin/python scripts/serve_mcp.py --base-url http://127.0.0.1:8874
```

该进程等待 MCP 客户端从标准输入发送协议消息；没有聊天式提示符是正常现象。标准输出用于协议，诊断信息走标准错误。实际使用时由 Claude Code 或 Codex 启动这个进程，不需要手动保持终端二。

## Claude Code 配置

将下面的 `/ABS/PATCHTRAIL` 全部替换为项目的绝对路径。可在调用方项目的 `.mcp.json` 中加入：

```json
{
  "mcpServers": {
    "patchtrail": {
      "type": "stdio",
      "command": "/ABS/PATCHTRAIL/.venv/bin/python",
      "args": [
        "/ABS/PATCHTRAIL/scripts/serve_mcp.py",
        "--base-url",
        "http://127.0.0.1:8874"
      ]
    }
  }
}
```

也可以由你自己运行以下命令添加本地配置，然后在 Claude Code 的 `/mcp` 中检查连接：

```sh
claude mcp add --transport stdio patchtrail -- \
  "/ABS/PATCHTRAIL/.venv/bin/python" \
  "/ABS/PATCHTRAIL/scripts/serve_mcp.py" --base-url http://127.0.0.1:8874
```

项目级 `.mcp.json` 的信任和启用流程以 [Claude Code 官方 MCP 文档](https://code.claude.com/docs/en/mcp) 为准。仓库提供配置示例，不替你修改个人配置或操作 Claude Code。

## Codex 配置

在 `~/.codex/config.toml` 或受信任项目的 `.codex/config.toml` 中加入；同样替换绝对路径：

```toml
[mcp_servers.patchtrail]
command = "/ABS/PATCHTRAIL/.venv/bin/python"
args = ["/ABS/PATCHTRAIL/scripts/serve_mcp.py", "--base-url", "http://127.0.0.1:8874"]
```

或由你自己使用 CLI 注册，并用 `codex mcp list` 检查：

```sh
codex mcp add patchtrail -- \
  "/ABS/PATCHTRAIL/.venv/bin/python" \
  "/ABS/PATCHTRAIL/scripts/serve_mcp.py" --base-url http://127.0.0.1:8874
```

配置格式和配置位置依据 [OpenAI 官方 MCP 文档](https://developers.openai.com/codex/mcp)。启动脚本自行定位仓库，不要求客户端设置特定工作目录，也不需要在 MCP 配置中放 Key 或管理员令牌。

## 一次演示怎么走

让调用方先执行 `list_tasks`，再调用 `start_demo_run`，例如参数为 `{"task_id":"quantity-shortfall","request_key":"mcp-demo-first-01"}`。拿到运行 ID 后，用 `get_run` 查询；看到待审批状态时，由人在本地网页审阅并批准，再查询实际测试结果。`after` 可使用上次看到的事件 ID，只读取后续事件。

适配器只通过既有 API 的匿名权限访问公共任务和演示记录，不直接打开 SQLite、不创建第二个 `RunService`，也不导入源码、传递认证头或读取 `.env`。私有导入、成员记录和真实模型运行沿用原 API 的拒绝规则；关闭公共演示时，运行相关调用仍须原 API 鉴权，MCP 不会绕过。

`--base-url` 只接受本机回环地址（`localhost` 或回环 IP），不接受账号、路径、查询参数或片段。HTTP 请求不使用环境代理、不跟随重定向，也不在调用间保留登录 Cookie。工具没有任意 URL、命令、模型模式、批准或认证参数。服务层的工具白名单、可编辑清单、预算、只读测试和容器检查继续生效。

协议与传输由[官方 SDK](https://py.sdk.modelcontextprotocol.io/run/)实现。本地测试和 SDK 协议检查不等于 Claude Code/Codex 真人客户端验收，也不代表新增模型修复效果。

本次交付的18项MCP专项检查和216项完整快照检查通过，包含两个真实stdio子进程连接模式；任务执行均使用假runner，详见[检查记录](../artifacts/mcp-stdio-validation.json)。
