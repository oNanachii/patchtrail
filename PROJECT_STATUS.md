# PatchTrail当前状态

2026年10月28日。第一主项目面向Agent应用和Python后端，同时保留全栈展示；用户本人操作Claude设计前端。

## 已验证

- 已实现本地清单导入、不可变私有源码快照、固定unittest/pytest、最多三文件原子补丁包、digest/版本审批、新副本OCI验证、diff及报告导出；网页载入令牌后显示私有项目。
- 用户Claude前端保留并扩展。最终116项Python检查、22项Node逻辑/SSR检查、TypeScript检查和生产构建通过；Python包装会调用部分Node检查，数量不能相加当作独立用例总数。
- v0.2真实DeepSeek验收使用两个仓库来源的三个预登记小功能，共五次尝试：两个任务完成审查、每项5条Podman测试及导出后独立复验；pytest任务两份提案因已知回归被审查拒绝，均未应用。首次合法subTest统计假阴性保留并修正。全部20次调用、88253 Token保留，不代表通用修复率，见artifacts/imported-live-validation.json。
- 此前v0.1三个已知自编回归任务的10轮调用、16998 Token和12条修复后用例证据继续保留，见artifacts/live-model-validation.json；它们不是v0.2自有项目验收，也不是盲评成绩。
- 当前运行v0.2.1，源码4113a23、独立release20261028-v02-02；后端仅监听回环3110，HTTPS8444。新版本公网三个脚本任务均经过实际Podman验证，各4条通过，未调用模型，见artifacts/v02-public-http-workflow.json。
- 首次live查询发生SQLite锁竞争，核对原run_id后继续验证，没有重建付费任务。增加同Store实例数据库操作串行化、存储忙503与有界只读重试，未声称解释全部锁根因或跨进程协调。
- 原本的脚本回归、真实容器、HTTP及桌面流程证据分别保存；密钥、数据库、完整live记录及服务器基线不进入Git。
- 独立复核修正令牌切换后的私有缓存/旧响应、未知创建结果重试的幂等键保留，以及容器检查证据不完整时的判定。浏览器使用真实私有后端完成新建、审阅、确认和报告下载；公网桌面1440×900、移动390×844读取实际结果均无页面横向溢出。见artifacts/frontend-independent-review.json与artifacts/public-https-validation.json。
- 可信TLS、全部11个静态文件逐字节匹配、CSP/noindex/API no-store、匿名live拒绝及私有读取401均通过。新代理开机启动与证书文件watcher已启用，手动reload保持PID且TLS正常；未强制未来ACME续期事件。
- 固定pytest镜像实际执行成功、失败、零测试和全跳过四种OCI场景；Linux真实两文件整包应用、两次容器检查和四种篡改拒绝通过。这些是自编运行时验收，不是模型成绩。
- 独立公网浏览器读取自有源码任务真实结果、21130 Token及5条Podman测试，桌面1440×900、移动390×844无全页横向溢出；私有任务强制live模式，清除令牌后私有任务和补丁立即遮蔽。下载接口与独立Git/OCI复验分开记录。

## 交付与待办

源码已公开：https://github.com/oNanachii/patchtrail 。当前CLI缺workflow scope，连接器写workflow也返回403；自动CI未启用，模板位于deploy/ci-workflow.yml.example。

公开演示：https://math.madokanun.top:8444 。当前交付见docs/V0_2_DELIVERY_2026-10-28.md；首次HTTPS部署历史见docs/PUBLIC_DEPLOYMENT_2026-10-28.md。数据、密钥、导入源码、完整运行记录和审批依据保留在私有目录，公开摘要只提供状态、统计和内容哈希。

docs/V0_2_SCOPE.md确定的工程范围已实现并按以上证据验证；用户明确授权了原三个源码任务及随后两次付费追加，全部尝试均保留。没有为了通过率应用审查已知不正确的提案，pytest模型任务仍明确为未完成；运行时自身的pytest支持已有独立实现和实际容器检查。

简历目标为一页通用主版，以PatchTrail和文本分类工作台为两个主项目，PantryPilot保留参赛备选。本人解释与关键修改验收、实体手机、Safari/Firefox、屏幕阅读器和用户视觉验收仍待完成；原投递简历保留。

DeepSeek API返回模型标识为deepseek-flash别名，未提供不可变模型权重快照。操作端确认由本轮Codex审查后调用确认接口完成，不等于用户或外部人员已验收。有限任务、运行时测试、真实模型效果、设备/设计验收和本人能力分别判断。
