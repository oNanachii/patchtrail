# PatchTrail

受控仓库排错与补丁验证工作台。选择一个失败任务，查看Agent读取代码和调用工具的过程，确认具体补丁，再由不可修改的测试决定检查结果。

[HTTPS公开演示](https://math.madokanun.top:8444) · [源码](https://github.com/oNanachii/patchtrail) · [部署验收](docs/PUBLIC_DEPLOYMENT_2026-10-28.md)。公开模式运行预置脚本，不调用模型；真实DeepSeek模式及其记录需要操作令牌。

项目面向Agent应用、Python后端和全栈交付实践。v0.3提供仅操作者可用的网页源码导入，保留原CLI。公开模式继续使用三个小型Python回归任务：库存数量缺口、补货覆盖旧批次日期、标签与历史非原子提交。它们是已知自编案例，不是盲评、SWE-bench成绩或通用代码修复能力证明。

![公开脚本运行示例 没有模型调用](artifacts/browser/v02/public-demo-desktop.png)

## 一次完整运行

1. 选择任务和执行方式。脚本演示不调用模型；DeepSeek模式会进行真实API调用。
2. 在受限任务副本中读取文件、搜索代码、复现原始测试失败。
3. 提出单文件或最多三文件补丁包，停在等待确认状态。
4. 操作者确认补丁digest及运行版本。
5. 从原始任务重建验证副本，应用准确补丁，在无网络、只读挂载、非root的受限容器中检查。
6. 保存状态、工具事件、diff和实际检查输出；成功状态由程序判定。

```mermaid
flowchart LR
  Q[任务排队] --> D[调查与工具调用]
  D --> A[等待补丁确认]
  A --> V[新工作区容器检查]
  V --> S[检查通过或失败]
  D --> F[预算或运行失败]
  D --> I[中断后明确恢复]
```

## 本地启动

Python 3.12或3.13。先进入本项目目录：

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock.txt
cp .env.example .env
```

在`.env`设置独立操作令牌及`DEEPSEEK_API_KEY`。配置文件由应用启动时读取，保存后需重启；不要覆盖已有配置。默认模型`deepseek-flash`与调用方式依据[DeepSeek官方工具调用文档](https://api-docs.deepseek.com/guides/tool_calls/)核对，模型可通过环境变量调整。

容器运行时需要Docker或兼容的Podman；预先拉取所选镜像，生产配置使用完整digest。运行请求不自动拉镜像。没有容器时可以查看界面和API，创建执行任务会明确返回503，不会转到宿主机执行。

```sh
.venv/bin/python -m uvicorn patchtrail.api:create_app --factory --host 127.0.0.1 --port 8874 --workers 1 --no-access-log
```

打开`http://127.0.0.1:8874`。首版只支持单API进程和单执行worker，不要添加多个uvicorn workers。状态与工作区默认保存在被Git忽略的`var/`。

## 执行边界

- 公开演示仅使用内置任务与受信脚本补丁；真实模型运行及其记录需要操作令牌。
- 操作者可在网页选择受限文本文件，预览并确认后导入私有源码快照；原清单CLI仍可用。匿名上传、ZIP/其他归档、远程Git URL、shell及任意命令不在范围内。
- 五个受控工具：有界读文件、字面搜索、固定检查、单文件提案、最多三文件补丁包。文件和工具结果作为数据，不视作用户指令。
- OCI容器禁网、只读根与工作区、非root、移除capabilities、no-new-privileges，并限制CPU、内存、进程、文件、时间和输出。
- 基线需确认实际执行了用例；验证需正数用例、退出码0、未超时/截断且测试输出正常结束。它是有限回归判定，不是对抗性防作弊证明。
- 重启后活动任务标为中断，待确认提案保留；恢复需明确操作。未知模型请求不自动重试，不保证精确一次外部执行。

## 导入自己的Python项目

### 网页导入

1. 右上角载入**操作令牌**，再点“新建运行”上方的“导入Python项目”。手机查看运行详情时，先点“全部运行”返回入口。
2. 选择项目文件夹，或选择多个文件。文件夹方式保留相对目录；多个文件按文件名平铺。填写需要修复的问题，检查测试入口及允许模型修改的源码文件，最多三个。
3. 点击“上传并预览”，将选中文件发送到当前后端服务器。核对文件、大小和测试设置，再点“确认导入”。网页自动选择新私有任务，无需SSH或手写清单。
4. 导入只保存快照，**不会执行源码，也不会自动调用DeepSeek**。真实运行须另行点击并确认。

上限为200个文件、总计2MiB、每文件128KiB。默认选择Python文件；常见私密/生成文件按名称排除，操作者仍须确认选中内容可以上传。测试和配置不能作为可修改源码。没有ZIP解包、远程仓库拉取或运行中安装依赖；当前仅提供标准库与已配置的pytest固定环境，项目额外依赖未满足时会明确停止。

预览保留24小时。相同内容的预览与确认可以沿原身份重试，未知确认结果不会自动新建模型运行；关闭后也可载入令牌到任务目录核对。清除或更换令牌会移除私有文件选择、预览与任务缓存。

### DeepSeek真实调用

网页的**操作令牌**是`PATCHTRAIL_ADMIN_TOKEN`，用于进入私有操作区；它不是`DEEPSEEK_API_KEY`。操作令牌由管理员提供，或在本机的私有`.env`配置中查找。DeepSeek Key保留在后端配置，网页不接收该Key。

载入操作令牌后，选择内置任务或自己的已导入任务，选择“DeepSeek真实调用”，点击“开始真实调用…”，再在确认窗口核对任务与费用。服务端会把本任务源码、测试和工具结果发送给配置的DeepSeek服务，并可能产生API费用。仅上传/确认导入不产生模型调用费用；已配置服务器Key时无需在网页重复填写。

模型提出补丁后会停在待审状态。先读diff，再点“确认并验证”；只有新副本中实际容器测试决定成功或失败。提案错误时可以取消，不应仅因模型说“修好了”就确认。

### 保留的命令行导入

也可准备清单，明确源码、不可修改测试、最多三个可修改文件及固定`unittest`或`pytest`路径。[清单模板](examples/import_manifest.example.json)继续可用。先预览，再确认：

```sh
.venv/bin/python -m patchtrail.import_project /path/to/owned-project --manifest /path/to/manifest.json --data-dir var
.venv/bin/python -m patchtrail.import_project /path/to/owned-project --manifest /path/to/manifest.json --data-dir var --commit
```

`--data-dir`必须与运行服务的数据目录一致。在服务器部署时以专用服务账户执行CLI，导入到`/var/lib/patchtrail/data`。导入建立不可变快照，原目录不改写；相同内容复用同一身份，源码或清单变化则是新任务。文件名排除不能识别源码中所有硬编码秘密，操作者须审阅允许文件的内容。

电脑上导入到`var`不会自动出现在公网网站。使用本项目的公网部署时，选定源码和清单须先放到服务器上、且服务账户可读；需要管理员的服务器命令行权限。进入`/opt/patchtrail/current`后，以`sudo -n -u patchtrail -H .venv/bin/python`运行相同导入模块，并将数据目录设为`/var/lib/patchtrail/data`。这段说明仅针对CLI：本地导入结果不会自动同步到服务器。使用上方网页“上传并预览”会明确发送选定文件；操作令牌不提供SSH权限。

网页载入操作令牌后会显示私有任务；导入任务仅支持真实模型模式。`pytest`需要操作者提前构建并固定专用镜像，见[镜像准备](deploy/pytest/README.md)；请求中不会安装依赖或拉取镜像。

审阅提案时可逐文件查看差异，按整包digest与版本确认。页面可下载`.diff`提案及JSON报告，导出本身不代表已通过检查；以运行状态与实际测试输出为准。模型结果及当前验收状态见[项目状态](PROJECT_STATUS.md)。

## 检查

前端采用React、TypeScript与Vite；构建后的`web/`已提交，部署时只需Python及容器运行时。修改前端或执行完整开发检查时，先安装锁定的Node依赖：

```sh
cd frontend
npm ci
npm test
npm run typecheck
npm run build
cd ..
```

```sh
.venv/bin/python -m unittest discover -s tests -v
```

单元与契约检查明确使用假runner/provider，检查状态和协议；fixture开发检查只执行受信自编源代码。实际OCI集成、真实DeepSeek效果、浏览器验收和用户面试讲解分别记录，不能合并成一个“全部通过”。当前结果与待验收项见[项目状态](PROJECT_STATUS.md)。自动CI模板位于`deploy/ci-workflow.yml.example`；启用需要仓库的workflow写入权限，尚未把本地通过当作CI通过。

## 首轮真实模型验收

2026年10月28日使用真实DeepSeek API，在预先选定的3个已知回归任务上各运行一次。模型自行调用工具并提出补丁，经本轮Codex代码审查及确认接口进入新的受限容器；每项原始版本均实际失败，修复后4条用例均通过。

| 已知任务 | 模型调用 | 实际Token | 修复后检查 |
| --- | ---: | ---: | --- |
| 库存数量缺口 | 3 | 5072 | 4条通过 |
| 补货批次到期 | 4 | 7005 | 4条通过 |
| 标签与历史事务 | 3 | 4921 | 4条通过 |

共10轮模型调用、16998 Token；未使用脚本provider生成这些补丁。API返回的模型标识为`deepseek-flash`别名，没有提供不可变权重快照。这是小范围已知任务的真实联通与回归结果，不支持通用修复准确率、未知任务表现或用户面试能力的结论。原始失败与修复、提案digest、源码版本和验证范围见[验收记录](artifacts/live-model-validation.json)。

首个任务的状态查询曾发生数据库锁竞争；核对原运行后继续验证，没有重复创建付费任务。随后增加同一Store实例的数据库操作串行化、可重试的存储忙响应和有界只读重试。跨进程或外部连接竞争仍需SQLite及运维处理，不能据此声称所有锁根因已解释。

## 交付材料

- [架构与取舍](docs/ARCHITECTURE.md)
- [Claude前端设计交接](docs/CLAUDE_FRONTEND_HANDOFF.md)与[可复制的启动提示](docs/CLAUDE_FRONTEND_PROMPT.txt)
- [面试讲解与独立验收](docs/INTERVIEW_WALKTHROUGH.md)
- [服务器部署与回退](deploy/README.md)
- [公网前端与HTTPS验收](docs/PUBLIC_DEPLOYMENT_2026-10-28.md)
- [自有项目导入与真实验收](docs/V0_2_DELIVERY_2026-10-28.md)
- OpenAPI契约：启动后的`/api/openapi.json`

前端由用户自行在Claude Code中完成设计，再经Codex独立检查和修正。已核对真实后端、补丁确认、容器检查、报告下载及桌面/移动宽度布局；未完成实体手机、屏幕阅读器或用户面试讲解验收。项目使用AI辅助开发，代码实现、用户关键判断及用户独立能力分别说明。

## 参考与许可

实现参考了[mini-swe-agent](https://github.com/SWE-agent/mini-swe-agent)的简洁控制流、[OpenHands Software Agent SDK](https://github.com/OpenHands/software-agent-sdk)的工作区与事件分离、[Inspect AI](https://github.com/UKGovernmentBEIS/inspect_ai)的任务/评分/日志分层。没有导入这些框架或沿用它们的成绩。源码归属与参考边界见[第三方说明](THIRD_PARTY_NOTICES.md)。本项目使用MIT许可。
