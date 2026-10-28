# PatchTrail当前状态

2026年10月28日。第一主项目面向Agent应用和Python后端，同时保留全栈展示；用户本人操作Claude设计前端。

## 已验证

- 后端工具循环、状态/事件、幂等、版本与digest审批、明确恢复、权限及受限OCI验证已实现。
- 用户Claude前端已交付并完成独立复核。最终集成77项Python检查、21项Node逻辑/SSR检查、TypeScript检查和生产构建通过；Node条目与Python调用包装不能相加当作98个互不重复用例。
- 真实DeepSeek在3个预先选定的已知自编任务上各运行一次：全部进入确认与实际容器检查，修复后共12条用例通过，10轮模型调用、16998 Token。见artifacts/live-model-validation.json；不支持通用或未知任务准确率。
- 后端及React前端已部署至独立frontend-01 release，控制面仍仅监听服务器回环3110。独立Caddy在8444提供HTTPS公开演示；原3个live结果保留。公网3个脚本任务全部经过真实Podman验证，各4条用例通过，未新增模型调用。
- 首次live查询发生SQLite锁竞争，核对原run_id后继续验证，没有重建付费任务。增加同Store实例数据库操作串行化、存储忙503与有界只读重试，未声称解释全部锁根因或跨进程协调。
- 原本的脚本回归、真实容器、HTTP及桌面流程证据分别保存；密钥、数据库、完整live记录及服务器基线不进入Git。
- 独立复核修正令牌切换后的私有缓存/旧响应、未知创建结果重试的幂等键保留，以及容器检查证据不完整时的判定。浏览器使用真实私有后端完成新建、审阅、确认和报告下载；公网桌面1440×900、移动390×844读取实际结果均无页面横向溢出。见artifacts/frontend-independent-review.json与artifacts/public-https-validation.json。
- 可信TLS、全部11个静态文件逐字节匹配、CSP/noindex/API no-store、匿名live拒绝及私有读取401均通过。新代理开机启动与证书文件watcher已启用，手动reload保持PID且TLS正常；未强制未来ACME续期事件。

## 交付与待办

源码已公开：https://github.com/oNanachii/patchtrail 。当前CLI缺workflow scope，连接器写workflow也返回403；自动CI未启用，模板位于deploy/ci-workflow.yml.example。

公开演示：https://math.madokanun.top:8444 。运行代码提交54ac264，部署记录见docs/PUBLIC_DEPLOYMENT_2026-10-28.md。原3个服务PID、启动时间与状态和启用前基线一致；仅新增8444/TCP监听及对应UFW规则，未改DNS。

下一步是本人完成一次操作、解释与关键修改验收，再据此定稿简历；现有简历未在本轮被替换。实体手机、Safari/Firefox、屏幕阅读器与用户视觉验收仍待完成。第二个互补业务全栈项目尚未选定。

DeepSeek API返回模型标识为deepseek-flash别名，未提供不可变模型权重快照。操作端确认由本轮Codex审查后调用确认接口完成，不等于用户或外部人员已验收。有限任务、运行时测试、真实模型效果、设备/设计验收和本人能力分别判断。
