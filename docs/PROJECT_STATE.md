# OpsSwarm 新窗口交接

更新于 2026-11-07。最小可信协作台与隔离 Compose 启动已验收。正常代表场景已回滚并形成恢复结果，事故收敛仍待验收。见[本地版本交付设计](architecture/local-multi-agent-release-scope.md)。

## 当前事实

- 仓库固定为`D:\PyCharm\job-project\ops-swarm`，使用仓库`.venv/Scripts/`。迁移 head 为 `0100_reflow_input_projection`，继续前核对 Git。
- 用户未跟踪目录`output/`、`tmp/`未读取、修改或提交。未升级用户Compose，不连接生产，不自动重启共享环境或修改内核参数。
- 现场 v3 以 graph-v16 接入精确窗口取证、工作摘要、共享预算及首轮可信经验。真实 PostgreSQL 与 checkpoint 用例已验证两轮调查、独立审查、中断恢复和终端交接。用例采用脚本模型与模拟 MCP 客户端，Compose 现场演示列入发布验收。见[现场动态协作记录](progress/live-dynamic-investigation.md)。
- graph-v16 的可信补充、输入准备和实际重评已接入候选方向。真实 PostgreSQL 与进程内演练服务验证了条件核验、审批、CAS 只派发一次、三次客观采样和事故收敛。重评中的其他补查意图由工程师接管。见[人工续接记录](progress/live-human-continuation.md)。
- graph-v16 回滚后明确未恢复时，可可信准入一次回流，在原预算和截止内完成新窗口取证、综合和独立审查。持久路由记录人工接管原因，事故继续调查，槽位归原运行。已在真实 PostgreSQL、脚本模型、固定变更响应及进程内演练服务中验收。见[现场失败回流记录](progress/live-reinvestigation-handoff.md)。
- 可信协作台接入现有事故详情，使用内存会话和授权 SSE。真实 PostgreSQL 验收复用现有 `ops-swarm-control-db-1` 的隔离验收库；已有业务库迁移停在 `0029_follow_up_governance_idx`。见[协作台记录](progress/trusted-collaboration-workroom.md)。
- 已有 OpsSwarm 容器满足验证条件时直接复用，需写入的验收使用该实例内的隔离数据库。
- 独立 `opsswarm-smoke` 已验收新库迁移、三类故障探针、真实 MCP 读取、保留卷后的事故读取与脚本模型驱动的现场 v3 单轮人工交接。前端本机端口为 15173；可信身份和 SSE 续读通过。见[本地启动记录](progress/local-compose-smoke.md)。
- 同一隔离环境已验证浏览器人工补充及准备命令、跨容器检查点恢复、人工重评到事故收敛，以及回滚生效后中断的只读对账。当时的失败回流在协调输入预算处受阻，已用增量策略处理。见[人工续接与中断记录](progress/local-compose-continuation.md)。
- 新现场运行的四阶段输入投影已通过隔离 Compose 失败回流代表场景。原预算下完成新取证、独立复查和持久人工接管，核对账本、槽位与唯一处置。进程内 API 长链的原回滚夹具仍触发 `release_dispatch_invalid`。见[回流输入投影记录](progress/reflow-input-projection.md)。
- 独立本地发布入口已通过新库迁移、完整装配、保留卷重启和浏览器补充及准备操作。常驻 Worker 已实际续接到候选评估，正常恢复代表场景先在派发前过期。省去一次重复来源读取后，复验到达远端注册，但返回 409 并进入 `unknown`。首次领取改用原生历史守卫后已缩短至约 1.8 秒，热栈复验曾在派发前来源重建时过期。复用已核验准备来源后，标准发布入口的新案例及时写入派发标记，随后远端注册响应超时，结果为 `unknown`。见[准备来源复验](progress/local-release-preparation-source-reuse.md)。见[发布入口验收](progress/local-release-entry.md)。
- 隔离执行 Worker 的 HTTP 等待调至 6 秒后，新事故在原 30 秒派发窗口内完成回滚，同键重放复用原任务，订单修订号只增加一次。正式恢复在首次订单采样超时后持久保留事故与活动槽位。见[远端注册及回滚复验](progress/local-release-registration-timeout.md)。
- 已按本会话授权重启 Docker Desktop，恢复原卷及续期的隔离测试身份，整栈就绪检查通过。观测超时后的新命令重试完成回滚，最终恢复尝试取得三份 200，但事故收敛为 `held_stale` 并保留槽位。标准 API 与执行入口均已恢复，环境时间调整单列。见[重启复验记录](progress/local-release-restart-verification.md)。
- 恢复来源读取复用 PostgreSQL 完整执行历史守卫，保留原请求契约及执行视图校验。单元、静态和 PostgreSQL 的并发接管、上游损坏拒绝、两类来源收尾与收敛定向回归通过。完整 Compose 正常恢复收敛待复验。见[恢复来源复用](progress/local-recovery-history-reuse.md)。
- 更新镜像后的案例在登记后失去租约，最终只读对账为 `not_applied`，修订 22 保留。执行状态 GET 的父级共享锁冲突已由最小探针复现，现改为一致性只读快照；读取与领取并发、历史损坏拒绝的 PostgreSQL 回归通过。见[状态快照记录](progress/local-release-status-snapshot.md)。
- 状态快照后的案例在创建发布准备请求时遇到发布头观测超时。独立四秒行锁探针验证发布头与固定订单 GET 的三秒超时、六秒成功且修订不变，隔离控制 API 与恢复采样器的 HTTP 等待均设为六秒。原运行预算已结束，完整行为用新案例复验。见[发布头等待记录](progress/local-release-observation-wait.md)。
- 六秒等待后的新案例在远端注册时超时，数据库守卫拒绝窗口外登记，修订 28 未回滚。资源处理后的新案例完成审批，随后写出前失效为 `blocked`，修订 30 未回滚，执行期间仍有明显内存/I/O压力。临时诊断已撤回，停止长链重跑，先处理验收环境资源。见[执行与资源诊断](progress/local-release-resource-diagnosis.md)。
- 调查与演练包的公开导出改为按需加载，纯数据模型不再隐式加载调查运行器或 HTTP 应用，兼容与相关单元、静态检查通过。完整执行 Worker 的历史工具 schema 校验仍需框架，尚未证明整栈内存下降。见[包入口整理](progress/local-package-import-boundary.md)。
- 141审批决定在da55c50提交，单元与迁移通过，完整API链未通过：批准在告警注入前提前拒绝尚未定位，后续诊断在131历史授权守卫中止；WSL/Docker时钟倒退另有实测，不能认定全部同根因。详见[141记录](progress/141-reinvestigation-approval-decision.md)。
- v1/v2、既有v3 graph-v15、131—140冻结历史及原累计预算语义保持。新现场策略不得静默升级旧运行、复用不适用审批或清零未知占用。

## 下一项完整行为

推进**可复现部署与发布**。恢复来源优化与执行状态快照已通过定向回归，发布头与采样器使用六秒传输等待。新案例仍在登记超时后安全停止，已观察到实际资源压力；先处理验收环境，再用新案例复验正常恢复收敛，随后核查已有业务卷的隔离迁移及演示。见[执行与资源诊断](progress/local-release-resource-diagnosis.md)及[交付设计](architecture/local-multi-agent-release-scope.md)。

部署时给执行 Worker 传入可信身份配置，并让恢复相关 Worker 在有效窗口内消费样本。失败回流的整栈证据见[投影验收](progress/reflow-input-projection.md)。

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

## 产品、兼容与评测入口

| 用途 | 入口与依赖 |
| --- | --- |
| 产品主线 | [Compose](../compose.yaml)装配控制 API、调查 Worker、MCP、人工准备和重评等进程。[动态 Worker 门禁](../src/opsswarm/worker/runner.py)和[现行图工厂](../src/opsswarm/worker/dynamic_factory.py)仅接 graph-v15 冻结与 graph-v16 现场；两者复用[三轮路由图](../src/opsswarm/investigation/dynamic_third_round_routing_graph.py)。现场执行、恢复和回流部分 Worker 目前仍按 [README](../README.md) 显式启动。 |
| v1/v2 调查兼容 | 同一个 [Worker 装配](../src/opsswarm/worker/main.py)继续路由既有单 Agent 与固定多 Agent；[固定任务图工厂](../src/opsswarm/worker/task_graph.py)仍有固定和现场任务的 checkpoint 回归。 |
| v3 graph-v1 至 graph-v14 | [旧工厂](../src/opsswarm/worker/dynamic_graph.py)每一版均有 `tests/integration/test_dynamic_*_graph.py` 直接引用，覆盖从协调、各轮专业任务到综合与审查的持久行为，其中部分复用现行图底层节点。旧图工厂没有接入默认动态 Worker；[版本与来源门禁](../src/opsswarm/investigation/workflow_versions.py)以及[迁移](../alembic/versions)继续承载旧记录和数据库契约，执行工厂退役需单独验证历史读取与剩余恢复回归。 |
| 冻结回流、审批与历史报告 | [回流协调至路由](../README.md)的显式入口同样是现场失败回流的生产路径，Python 重建与 [0087–0092 迁移](../alembic/versions)的 PostgreSQL 守卫共同绑定历史材料；后续候选、计划及审批有独立 API、迁移与测试。141 的验收欠项见上。 |
| 发布脚本 | [MVP 验证](../scripts/verify-mvp.ps1)及[演示脚本](../scripts/demo-mvp-flow.ps1)仍是历史发布复现入口，由 [README](../README.md)引用。现场 Compose 验收的缓存脚本依赖隔离演练环境，详见[本地续接记录](progress/local-compose-continuation.md)。 |
| 独立评测 | [版本化评测](../src/opsswarm/evaluation/main.py)、[真实模型单例](../src/opsswarm/evaluation/real_main.py)与[批量入口](../src/opsswarm/evaluation/real_batch_main.py)复用固定样本、版本化报告和评测持久化；[RCAEval RE2](../evaluations/rcaeval-re2)独立维护冻结协议、回放和配对实验入口。保留既有数据、脚本和报告。 |

首批核对未删除代码。四阶段输入实测与回放差异见[回流整改基线](progress/reflow-remediation-baseline.md)。

## 开发与验证偏好

按完整业务行为组织交付，不逐个内部状态追加发布前置，不顺带重构历史模块。必要权限、预算、事务、幂等、租约及审批绑定检查保留；下层已验证规则不在上层重复穷举，同一代码通过一次即可。同根因两次长链失败后改最小探针，相关修复或新证据出现再定向复验。真实模型调用须本会话明确授权。
