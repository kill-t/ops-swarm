# OpsSwarm 新窗口交接

更新于 2026-11-05。现场一次失败回流与安全交接已通过代表链验收。下一项为最小可信协作台。见[本地版本交付设计](architecture/local-multi-agent-release-scope.md)。

## 当前事实

- 仓库固定为`D:\PyCharm\job-project\ops-swarm`，使用仓库`.venv/Scripts/`。迁移 head 为 `0099_live_reflow_handoff`，继续前核对 Git。
- 用户未跟踪目录`output/`、`tmp/`未读取、修改或提交。未升级用户Compose，不连接生产，不自动重启共享环境或修改内核参数。
- 现场 v3 以 graph-v16 接入精确窗口取证、工作摘要、共享预算及首轮可信经验。真实 PostgreSQL 与 checkpoint 用例已验证两轮调查、独立审查、中断恢复和终端交接。用例采用脚本模型与模拟 MCP 客户端，Compose 现场演示列入发布验收。见[现场动态协作记录](progress/live-dynamic-investigation.md)。
- graph-v16 的可信补充、输入准备和实际重评已接入候选方向。真实 PostgreSQL 与进程内演练服务验证了条件核验、审批、CAS 只派发一次、三次客观采样和事故收敛。重评中的其他补查意图由工程师接管。见[人工续接记录](progress/live-human-continuation.md)。
- graph-v16 回滚后明确未恢复时，可可信准入一次回流，在原预算和截止内完成新窗口取证、综合和独立审查。持久路由记录人工接管原因，事故继续调查，槽位归原运行。已在真实 PostgreSQL、脚本模型、固定变更响应及进程内演练服务中验收。见[现场失败回流记录](progress/live-reinvestigation-handoff.md)。
- 141审批决定在da55c50提交，单元与迁移通过，完整API链未通过：批准在告警注入前提前拒绝尚未定位，后续诊断在131历史授权守卫中止；WSL/Docker时钟倒退另有实测，不能认定全部同根因。详见[141记录](progress/141-reinvestigation-approval-decision.md)。
- v1/v2、既有v3 graph-v15、131—140冻结历史及原累计预算语义保持。新现场策略不得静默升级旧运行、复用不适用审批或清零未知占用。

## 下一项完整行为

推进**最小可信协作台**。在现有事故详情接入可信身份、调查证据和处置状态，支持必要人工操作，展示一次回流后的人工交接及原因。验证关键角色权限、刷新与 SSE 重连。

随后装配可复现部署并完成发布验收。依赖、必要测试及三条场景见[交付设计](architecture/local-multi-agent-release-scope.md)。

141及时间问题单列：先用最小探针定位首个拒绝边界，无相关修复或新证据不再整链复跑。不纳入本版的回流新方案再审批执行保持未验收；影响首发主链的安全或时钟问题仍阻断发布。全部本版必需项通过并完成演示/发布后结束，其余进入后续版本。

## 最小阅读与代码入口

先读[AGENTS](../AGENTS.md)、本文、[进度索引](progress/README.md)和[当前交付设计](architecture/local-multi-agent-release-scope.md)。长期系统规则按需查[64路线](architecture/64-full-multi-agent-roadmap.md)/[65设计](architecture/65-agent-system-design.md)，不逐篇重读历史。

| 入口 | 用途 |
| --- | --- |
| [v3门禁](../src/opsswarm/investigation/workflow_versions.py)、[动态图](../src/opsswarm/investigation/dynamic_third_round_routing_graph.py) | 现场新策略及多轮协作的实际边界 |
| [Worker装配](../src/opsswarm/worker/main.py)、[MCP客户端](../src/opsswarm/telemetry_mcp/client.py) | 数据源、模型与独立Worker的集成位置 |
| [经验上下文](../src/opsswarm/experience/context_store.py)、[预算仓储](../src/opsswarm/investigation/budget/store.py) | 首调冻结、共享额度及历史引用 |
| [人工重评](../src/opsswarm/investigation/human_review/worker.py)、[候选来源](../src/opsswarm/investigation/candidate/source.py) | 人工输入后的实际继续 |
| [执行Worker](../src/opsswarm/execution/release_jobs/worker.py)、[恢复判据](../src/opsswarm/verification/candidate/policy.py) | 原操作对账与客观业务样本 |
| [141设计](architecture/141-reinvestigation-approval-decision.md)、[141记录](progress/141-reinvestigation-approval-decision.md) | 已提交能力及未验收欠项，不是自动下一步 |

## 开发与验证偏好

按完整业务行为组织交付，不逐个内部状态追加发布前置，不顺带重构历史模块。必要权限、预算、事务、幂等、租约及审批绑定检查保留；下层已验证规则不在上层重复穷举，同一代码通过一次即可。同根因两次长链失败后改最小探针，相关修复或新证据出现再定向复验。真实模型调用须本会话明确授权。
