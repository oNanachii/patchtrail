# PatchTrail

**带人工审批与沙箱验证的 AI 代码修复 Agent。**

Python · FastAPI · SQLite · DeepSeek 函数调用 · Docker / rootless Podman · React + TypeScript

[在线演示](https://patchtrail.madokanun.top) · [3 分钟上手](docs/BYOK_QUICKSTART.md) · [架构与取舍](docs/ARCHITECTURE.md) · [版本历史](docs/HISTORY.md)

## 解决什么问题

AI 改代码时常不先复现问题、甚至改测试来“通过”，结果难以信任。PatchTrail 先在容器中复现失败，再让 Agent 调查并提出补丁；人工批准具体补丁后，由隔离容器中不可修改的测试判定是否修好。

![PatchTrail 在线演示：开始演示，Agent 调查并提出补丁，审阅 diff 后批准，容器中运行测试，失败的测试变为通过](docs/images/patchtrail-demo.gif)

<sub>录自线上公开演示，未登录、未填写任何 Key。演示补丁由预先写好的脚本给出（不调用模型），测试在真实容器中运行。</sub>

## 怎么工作

```mermaid
flowchart LR
  U[上传源码、测试和问题] --> B[容器中复现失败]
  B --> C[确认调用模型]
  C --> D[Agent 调查并提出补丁]
  D --> A[人工审阅并批准]
  A --> V[新副本中运行原测试]
  V --> S[通过后下载修复文件]
```

原测试没有失败（或缺环境、超时）就不调用模型；批准后的测试没有通过，就不提供修复文件。

## 关键设计

1. **只给模型 5 个受限工具，并限定预算。** 基于 DeepSeek 函数调用，模型只能读文件、搜索代码、运行测试、提交单文件补丁、提交最多 3 个文件的补丁包，每次调查最多 8 次模型调用、12 步工具调用、120 秒；这样模型不能执行任意命令或修改测试，跑偏时花费也有上限。
2. **批准的补丁就是被验证的补丁。** FastAPI + SQLite 状态机把每次状态变化和事件写进同一个事务，创建任务的请求带幂等键，补丁带 SHA-256 指纹，批准时还要匹配运行记录的版本号（乐观锁）；所以重复点击不会重复创建付费任务，批准前补丁或状态变了会被拒绝，服务重启后也能恢复中断的任务。
3. **复现和验证都在隔离容器里。** 调用模型前先在容器中确认原测试确实失败，批准后在新副本中运行测试，容器禁网、非 root、只读文件系统，并限制 CPU、内存和进程数（线上用 rootless Podman）；只有测试通过才提供修复文件下载，模型自己说“修好了”不算数。

## 结果

| 指标 | 结果 | 出处 |
| --- | --- | --- |
| 真实模型（DeepSeek）完成的自建任务 | **6 个中完成 5 个**：修 bug 3/3，加小功能 2/3；完成的任务都在新容器中通过了全部原测试 | [修 bug](artifacts/live-model-validation.json#L118-L124) · [加小功能](artifacts/imported-live-validation.json#L4-L17) |
| 审查环节拒绝的补丁 | 另 1 个任务的 **2 份**补丁因引入回归被拒，均未应用 | [拒绝原因](artifacts/imported-live-validation.json#L651) |
| 自动化测试 | **149** 项 Python、**53** 项前端 | [项目状态](PROJECT_STATUS.md?plain=1#L17) |

## 局限

- 6 个任务都是自建的小任务，不代表通用修复率；验收中的补丁审查由 AI 代码审查（Codex）完成，不是外部人工验收。
- 只支持小型 Python 项目（标准库或预先准备的 pytest 环境），不自动安装依赖；一次最多导入 200 个文件、2 MiB。
- 邀请制 beta，单机、单个执行 worker；容器隔离降低了风险，但不是对任意陌生代码的安全认证。

## 在线演示和快速开始

**在线演示：<https://patchtrail.madokanun.top>**。点“一键体验脚本演示”就能走完上面的流程，不用注册、不用填 Key；演示补丁是预先写好的（不调用模型），测试在真实容器里运行。想用自己的项目，需要管理员给的邀请码和自己的 DeepSeek Key（平台不收费，模型费用由你的 DeepSeek 账户承担），步骤见 [3 分钟上手](docs/BYOK_QUICKSTART.md)。

**本地运行**（Python 3.12 或 3.13，另需 Docker 或 Podman）：

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock.txt
cp -n .env.example .env
docker pull python:3.12-slim   # 测试用的镜像；创建任务时不会自动拉取
.venv/bin/python -m uvicorn patchtrail.api:create_app --factory --host 127.0.0.1 --port 8874 --workers 1 --no-access-log
```

打开 <http://127.0.0.1:8874>。用 Podman 时把 `.env` 里的 `PATCHTRAIL_CONTAINER_BIN` 改成 `podman`。没有容器运行时也能浏览界面和 API，但创建任务会直接返回 503，不会退回到本机执行代码。服务只支持单进程（`--workers 1`），数据默认写在被 Git 忽略的 `var/`；对外提供服务必须走 HTTPS。

**运行测试**（前端构建产物 `web/` 已提交，只有改前端时才需要 Node）：

```sh
.venv/bin/python -m unittest discover -s tests -v
cd frontend
npm ci
npm test
npm run typecheck
npm run build
cd ..
```

## 管理员：邀请码与命令行导入

成员用邀请码和自己的 Key 登录。最初的部署管理员仍可展开登录窗口里的“已有操作者入口”，填写 `PATCHTRAIL_ADMIN_TOKEN`，读取原来的私有任务和历史，并使用服务器上的模型配置；两类身份的数据互不合并，管理员令牌也不是邀请码。

邀请码在服务器本地管理。先进入项目目录（或把项目路径加入 `PYTHONPATH`），用能访问服务数据目录和目标私有文件目录的服务账户执行。下面的路径只是部署示例，输出文件必须是尚不存在的新文件：

```sh
cd /opt/patchtrail/current
.venv/bin/python -m patchtrail.members create --data-dir /var/lib/patchtrail/data --output /path/to/new-private-invitation.json
.venv/bin/python -m patchtrail.members revoke --data-dir /var/lib/patchtrail/data --member-id '<member-id-from-private-file>'
```

创建命令只打印完成说明，邀请码只写入新建的 0600 权限文件，服务端只保存邀请码的摘要。请私下发送邀请码，不要把文件内容、邀请码、Key 或成员 ID 放进仓库、终端日志或公开文档。成员一般不需要 SSH。

管理员也可以用命令行按清单导入项目（[清单模板](examples/import_manifest.example.json)）：不加 `--commit` 只预览，加上才真正导入。同样要在项目目录运行，数据目录与服务一致：

```sh
.venv/bin/python -m patchtrail.import_project /path/to/owned-project --manifest /path/to/manifest.json --data-dir /path/to/service-data
.venv/bin/python -m patchtrail.import_project /path/to/owned-project --manifest /path/to/manifest.json --data-dir /path/to/service-data --commit
```

这样导入的项目只属于管理员空间，不会同步到成员空间或其他服务器；成员请用网页导入。pytest 镜像由管理员预先准备，见[镜像说明](deploy/pytest/README.md)。

## 文档

- [3 分钟上手](docs/BYOK_QUICKSTART.md)：在线演示和用自己项目的步骤
- [架构与取舍](docs/ARCHITECTURE.md)、[项目讲解](docs/PROJECT_EXPLANATION.md)（另有 [PDF 版](output/pdf/PatchTrail_项目讲解_2026-10-29.pdf)，对应 v0.3.1）
- [API 约定](docs/BYOK_API_CONTRACT.md)；服务启动后可查看 `/api/openapi.json`
- [服务器部署与回退](deploy/README.md)
- [版本历史与详细说明](docs/HISTORY.md)：域名变更、版本说明、各版本验收记录和详细的使用边界；当前部署状态见[项目状态](PROJECT_STATUS.md)
- [AI 协作过程文件](docs/process/)：交接文档、启动提示和交付记录。本项目借助 AI 编程助手开发。

## 参考与许可

实现参考了 [mini-swe-agent](https://github.com/SWE-agent/mini-swe-agent) 的简洁控制流、[OpenHands Software Agent SDK](https://github.com/OpenHands/software-agent-sdk) 的工作区与事件分离、[Inspect AI](https://github.com/UKGovernmentBEIS/inspect_ai) 的任务、评分和日志分层；没有引入这些框架，也没有沿用它们的评测成绩。源码归属与参考边界见[第三方说明](THIRD_PARTY_NOTICES.md)。本项目使用 MIT 许可。
