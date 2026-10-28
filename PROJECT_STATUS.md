# PatchTrail当前状态

2026年10月28日。第一主项目面向Agent应用和Python后端，同时保留全栈展示；用户本人操作Claude设计前端。

## 已验证

- 后端工具循环、状态/事件、幂等、版本与digest审批、明确恢复、权限及受限OCI验证已实现。
- 74项后端独立候选检查通过。候选保留已提交的功能前端基线，用户Claude正在修改的前端不纳入本次后端验收。
- 真实DeepSeek在3个预先选定的已知自编任务上各运行一次：全部进入确认与实际容器检查，修复后共12条用例通过，10轮模型调用、16998 Token。见artifacts/live-model-validation.json；不支持通用或未知任务准确率。
- 后端可靠性修正已部署至独立backend-03 release，仍仅监听服务器回环3110。原3个live结果保留；修正后一个脚本任务的实际HTTP/OCI链路通过，没有新增模型调用，见artifacts/post-live-reliability-smoke.json。
- 首次live查询发生SQLite锁竞争，核对原run_id后继续验证，没有重建付费任务。增加同Store实例数据库操作串行化、存储忙503与有界只读重试，未声称解释全部锁根因或跨进程协调。
- 原本的脚本回归、真实容器、HTTP及桌面流程证据分别保存；密钥、数据库、完整live记录及服务器基线不进入Git。

## 交付与待办

源码已公开：https://github.com/oNanachii/patchtrail 。当前CLI缺workflow scope，连接器写workflow也返回403；自动CI未启用，模板位于deploy/ci-workflow.yml.example。

用户Claude产生的frontend/web/相关测试与设计文件保留在工作区，未随本次后端提交发布。下一步是独立复核前端，再完成公网HTTPS入口、公开演示和本人面试关键修改验收。第二个互补业务全栈项目尚未选定。

DeepSeek API返回模型标识为deepseek-flash别名，未提供不可变模型权重快照。操作端确认由本轮Codex审查后调用确认接口完成，不等于用户或外部人员已验收。有限任务、运行时测试、真实模型效果、设备/设计验收和本人能力分别判断。
