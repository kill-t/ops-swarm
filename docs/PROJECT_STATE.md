# OpsSwarm 新窗口交接

更新：2026-11-05。当前收口为“可演示、可恢复的多 Agent 本地事故处置版本”，核心优先，范围见[交付设计](architecture/local-multi-agent-release-scope.md)。本次只完成文档决策，新增运行行为尚未验收；不按142及后续编号自动顺延。

## 当前事实

- 仓库固定为`D:\PyCharm\job-project\ops-swarm`，使用仓库`.venv/Scripts/`。本次文档决策基线main的da55c50；迁移head仍为`0096_reflow_approval_decision`，继续前核对Git。
- 用户未跟踪目录`output/`、`tmp/`未读取、修改或提交。未升级用户Compose，不连接生产，不自动重启共享环境或修改内核参数。
- 冻结v3已有三轮动态专业任务、逐轮综合/独立审查、确定性路由、checkpoint及Worker终端交接；现场v2已有日志/指标只读MCP。当前v3启动和首轮协调经验仍有冻结模式门禁，现场v3不是已交付事实。
- 已有任务上下文、工作摘要、材料清单、共享预算及可信经验生命周期，经验已实际进入冻结v3首轮协调；现场消费仍需兼容策略和集成。
- 人工等待已有可信补充/取消、独立输入准备、实际模型重评和候选方向消费；不能写成人工只存命令，也不能写成旧graph-v15图内interrupt/resume。后续所有follow_up方向并未全部消费。
- 原候选链已有可信审批、条件核验、演练CAS执行、业务探针、父运行收尾及Incident收敛；首次失败回流已有协调、专业新取证、综合及独立审查。这些有组件历史验收，尚不能替代统一现场版本的部署/界面/恢复验收。
- 141审批决定在da55c50提交，单元与迁移通过，完整API链未通过：批准在告警注入前提前拒绝尚未定位，后续诊断在131历史授权守卫中止；WSL/Docker时钟倒退另有实测，不能认定全部同根因。详见[141记录](progress/141-reinvestigation-approval-decision.md)。
- v1/v2、既有v3 graph-v15、131—140冻结历史及原累计预算语义保持。新现场策略不得静默升级旧运行、复用不适用审批或清零未知占用。

## 下一项完整行为

先交付**现场动态协作**：确定现场v3的状态/接口/持久来源、兼容策略、上下文与预算/经验绑定、失败和恢复边界，再接通受限MCP及实际多轮调查。复用已有领域与端口，不通过解除冻结门禁冒充完成，不新建通用Harness或编排平台。

随后打通人工实际续接与单步骤客观恢复、一次失败再调查和明确交接，最后补最小可信协作台与可复现部署。依赖、必要测试及三条场景统一见[交付设计](architecture/local-multi-agent-release-scope.md)，不在交接重复维护清单。

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
