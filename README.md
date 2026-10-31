# PatchTrail

受控的小型Python排错与补丁验证工作台。把代码、测试和问题交给一个有界Agent，先复现失败，再审阅补丁，由实际容器测试决定是否通过；通过后下载可应用的修复文件。

[HTTPS公开演示](https://patchtrail.madokanun.top) · [源码](https://github.com/oNanachii/patchtrail) · [三分钟演示与使用步骤](docs/BYOK_QUICKSTART.md) · [当前状态](PROJECT_STATUS.md)

2026-10-31启用独立域名，无需输入端口。旧演示入口继续保留，任务和运行记录共用原数据；换到新网址后，需要重新连接邀请码和自己的Key。

当前为 **v0.4.0邀请制有限beta**，运行源码`8fcbaca`。公开脚本演示不需要Key、不调用模型；受邀成员使用自己的DeepSeek Key，平台不收费，真实模型费用由自己的DeepSeek账户承担。成员任务与记录独立隔离，原操作者入口及历史记录保留。当前边界不等于面向任意陌生代码的商用安全认证。

## 三步使用自己的项目

1. **邀请码＋自己的Key连接。** 通过HTTPS打开页面，点击“连接访问”，填写管理员单独提供的邀请码和自己的DeepSeek Key。连接只建立短期会话，不调用模型；连接成功不等于Key已通过DeepSeek验证。
2. **导入并预检。** 选择源码、测试文件并填写问题，预览清单后确认导入，再点“运行预检（不调用模型）”。只有完整、有效的原始测试失败才允许继续；已通过、零测试、缺环境、超时或未确认结果都会阻止模型请求。
3. **确认调用、审阅、下载。** 核对任务和费用后明确开始真实调用；Agent提出补丁后暂停，审阅diff再确认验证。只有实际验证成功才提供“修复文件ZIP”，包含原相对路径的修改文件、原文件摘要和应用说明。先备份并核对原文件版本，再复制到自己的项目；工作台不会自动改动原仓库。

源码上传、预检与模型调用是不同步骤。导入会把选中文件发送到本工作台；确认真实调用后，任务源码、测试和工具结果才发送给DeepSeek官方接口。不要上传无权共享的代码或凭据。浏览器凭据只在页内内存，服务端Key只在短期会话内存；默认会话1小时，退出或过期后停止新的模型请求。已经发出的请求仍可能完成并计费，未知结果不自动重试。

只想了解流程，可以直接使用公开的三个脚本任务。脚本补丁是预先编写的，实际检查仍由容器执行；它证明运行与验证流程，不是模型修复效果。详见[快速演示](docs/BYOK_QUICKSTART.md)。

```mermaid
flowchart LR
  U[导入文件与问题] --> B[固定容器预检]
  B --> C[确认真实模型调用]
  C --> D[调查与提出补丁]
  D --> A[审阅并确认]
  A --> V[新副本容器验证]
  V --> S[通过后下载修复文件]
```

原[项目讲解](docs/PROJECT_EXPLANATION.md)与[8页PDF](output/pdf/PatchTrail_项目讲解_2026-10-29.pdf)保留架构和历史验收说明，其中操作令牌流程属于v0.3；当前成员步骤以上方及快速指南为准。[最小导入例子](examples/web-import-quickstart/)是自编练习，不是未知任务评测。

## 本地启动

Python 3.12或3.13。先进入本项目目录：

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock.txt
cp -n .env.example .env
```

成员BYOK不需要配置共享模型Key。管理员可以保留独立`PATCHTRAIL_ADMIN_TOKEN`与`DEEPSEEK_API_KEY`供旧operator流程使用；不要向成员发放这个令牌。`.env`由应用启动时读取，已有配置不要覆盖。模型由服务器选择，连接只允许DeepSeek官方端点，不接受成员自定义base URL。

容器运行时需要Docker或兼容的Podman；预先拉取所选镜像，生产配置使用完整digest。运行请求不自动拉镜像。没有容器时可以查看界面和API，创建执行任务会明确返回503，不会转到宿主机执行。

```sh
.venv/bin/python -m uvicorn patchtrail.api:create_app --factory --host 127.0.0.1 --port 8874 --workers 1 --no-access-log
```

打开`http://127.0.0.1:8874`。HTTP仅用于目标与真实客户端均为回环地址的本机开发，公网成员连接必须HTTPS；不信任任意客户端转发头。只支持单API进程，全体成员、operator和demo共用一个有界执行worker，不要添加多个uvicorn workers。状态与工作区默认保存在被Git忽略的`var/`。

## 支持范围与额度

- 适用于小型Python标准库项目，以及服务器已准备固定pytest环境的项目；一次最多修改三个源码文件，测试和配置不可修改。
- 导入最多200文件、总计2MiB、每文件128KiB，只接受明确选择的UTF-8文本文件。没有ZIP导入、远程Git、任意shell、自动依赖安装、公开注册或支付功能。
- OCI检查禁网、只读根与工作区、非root，并限制CPU、内存、进程、时间和输出；容器不可用时停止，不回退到宿主机执行代码。
- 预检绑定成员、任务源码与检查配置，有效期15分钟；登录、导入和预检均不调用模型。成功验证要求有效的正数用例、正常结束、退出码0且未超时或截断。这是有限回归判定，不是对抗性防作弊证明。
- 默认全局最多6个未终结运行，每成员最多2个未终结运行、累计20个运行和5个导入快照。共享队列满时明确拒绝，不为每个成员另开worker绕过上限。
- 导入预览保留24小时，重复确认沿同一预览核对。成员用同一邀请码重新连接仍看到自己的历史；退出、过期或撤销不能被当作删除记录。撤销邀请会取消其未终结运行并释放额度。
- 成员空间与旧operator空间分开，不能读取其他成员或legacy私有任务。进程重启后的恢复需要明确操作，未知外部请求不自动重试，也不承诺精确一次执行。

## 管理员与旧operator入口

新成员使用邀请码＋自己的Key。原部署操作者可在连接窗口的兼容入口验证`PATCHTRAIL_ADMIN_TOKEN`，继续读取原私有任务与历史，并使用原服务器模型配置。两个身份空间不会合并，管理员令牌不是邀请码。

邀请码在服务器本地管理。**先进入项目目录**，或显式把项目路径加入`PYTHONPATH`；以有权访问服务数据目录及目标私有文件目录的服务账户执行。以下路径是部署示例，输出文件必须尚不存在：

```sh
cd /opt/patchtrail/current
.venv/bin/python -m patchtrail.members create --data-dir /var/lib/patchtrail/data --output /path/to/new-private-invitation.json
.venv/bin/python -m patchtrail.members revoke --data-dir /var/lib/patchtrail/data --member-id '<member-id-from-private-file>'
```

创建命令只打印完成说明，凭据仅写入新建0600文件；注册库仅保存邀请码摘要。通过私下渠道交付邀请码，不将文件内容、邀请码、Key或成员ID发布到仓库、终端日志或公开文档。成员通常不需要SSH。

旧operator清单CLI继续保留；[清单模板](examples/import_manifest.example.json)支持预览与明确提交。也必须在项目目录运行，数据目录与服务一致：

```sh
.venv/bin/python -m patchtrail.import_project /path/to/owned-project --manifest /path/to/manifest.json --data-dir /path/to/service-data
.venv/bin/python -m patchtrail.import_project /path/to/owned-project --manifest /path/to/manifest.json --data-dir /path/to/service-data --commit
```

这类导入属于指定数据目录中的operator空间，不会自动同步到成员空间或其他服务器。成员请使用网页导入。pytest镜像由管理员预先准备，见[镜像说明](deploy/pytest/README.md)。完整新接口见[BYOK契约](docs/BYOK_API_CONTRACT.md)。

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

## v0.4已验证与仍待验证

本地149项Python、53项Node、TypeScript与生产构建通过；独立TLS修复后9项定向复核通过。实际公网HTTPS验证使用已有受邀身份与**非真实模型Key**：登录、Podman预检4条用例、缺预检的live请求拒绝、成员脚本流程修复后4条用例、ZIP实际文件内容、匿名读取拒绝、退出后旧会话拒绝均通过。本轮HTTP验收摘要（原始记录保留在操作者本地）记录本轮付费模型调用为0，没有为了验收增加额外账号。

这些结果区分了真实OCI与测试替身，没有新增真实DeepSeek修复成绩。本机浏览器已检查成员连接、预检、费用窗口取消和退出；公网完整浏览器流程的最终结果另行记录，不提前视作完成。邀请制beta、实际模型效果、视觉/设备验收和本人讲解分别判断。

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

原前端由用户自行在Claude Code中设计，再经Codex独立检查和修正。旧版本的后端、补丁确认、容器检查、报告下载及桌面/移动宽度布局证据保留；新v0.4验证范围以上方当前记录为准，实体手机、屏幕阅读器或用户面试讲解仍未完成验收。项目使用AI辅助开发，代码实现、用户关键判断及用户独立能力分别说明。

## 参考与许可

实现参考了[mini-swe-agent](https://github.com/SWE-agent/mini-swe-agent)的简洁控制流、[OpenHands Software Agent SDK](https://github.com/OpenHands/software-agent-sdk)的工作区与事件分离、[Inspect AI](https://github.com/UKGovernmentBEIS/inspect_ai)的任务/评分/日志分层。没有导入这些框架或沿用它们的成绩。源码归属与参考边界见[第三方说明](THIRD_PARTY_NOTICES.md)。本项目使用MIT许可。
