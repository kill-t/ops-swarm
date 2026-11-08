# OpsSwarm 项目交接

更新于 2026-11-08。当前目标是整改并收口可演示、可恢复的本地多 Agent 版本。交付边界以[本地版本范围](architecture/local-multi-agent-release-scope.md)为准，历史验收记录保留原有含义。

## 当前状态

- 仓库固定为 `D:\PyCharm\job-project\ops-swarm`，使用仓库 `.venv/Scripts/`。迁移 head 为 `0100_reflow_input_projection`，继续前核对 Git。
- 按用户要求，`opsswarm-local-release` 全栈与 `release-fixture-model` 均已停止，容器和数据卷保留。未关闭 Docker Desktop 或 WSL，不自动恢复服务、重跑长链或调整共享系统。
- 当前整改已完成现行图与历史装配分离、历史工厂去重及首页收口。见[入口分离](progress/current-dynamic-factory-boundary.md)和[整批整改](progress/legacy-assembly-and-project-entry-cleanup.md)。下一批按完整问题推进，避免将内部操作拆成多轮交付。
- 用户未跟踪目录 `output/`、`tmp/` 未读取、修改或提交；不访问原 RAG 仓库。已有 OpsSwarm 容器满足条件时优先复用，需要写入的验收使用隔离数据库。

## 能力与验收边界

| 能力 | 已确认事实与来源 |
| --- | --- |
| 现场动态调查 | graph-v16 已接入动态专业分工、精确窗口取证、摘要、共享预算、首轮可信经验与终端交接。真实 PostgreSQL 和 checkpoint 验证使用脚本模型及模拟 MCP；见[现场协作](progress/live-dynamic-investigation.md) |
| 人工续接与单步骤处置 | 可信补充、实际重评、候选、核验、审批、唯一 CAS、客观采样及收敛已有真实 PG 和进程内演练代表链。见[人工续接](progress/live-human-continuation.md) |
| 一次失败回流 | 现场新窗口取证、综合、独立复查及持久人工接管已在隔离 Compose 代表场景通过；原预算、账本、槽位和唯一操作保持。见[输入投影与整栈验证](progress/reflow-input-projection.md) |
| 协作台与部署 | 最小可信协作台通过组件/API 验收，隔离 Compose 通过新库部署、保留卷读取、浏览器补充准备及指定跨容器续接。见[协作台](progress/trusted-collaboration-workroom.md)、[启动](progress/local-compose-smoke.md)、[续接与中断](progress/local-compose-continuation.md)、[发布入口](progress/local-release-entry.md) |
| 正常发布完整行为 | 曾完成回滚和正式采样，但标准入口的事故收敛与槽位释放仍未完整通过。来源读取、状态快照和提前采样等待有定向修复；见[来源遍历](progress/local-release-source-traversal.md)、[状态快照](progress/local-release-status-snapshot.md)、[采样等待](progress/local-recovery-sample-wait.md) |
| 环境与其他欠项 | WSL 重置及授权停服务对照未消除 UTC 倒退，服务已恢复原状；见[校时核查](progress/local-wsl-clock-source.md)。141 单元与迁移通过、完整 API 未验收，不能把其提前拒绝全部归因于时钟；见[原记录](progress/141-reinvestigation-approval-decision.md) |

以上组件或指定场景成功均不代表当前完整版本已发布，不将脚本模型结果写成真实模型推理效果。已有业务卷曾停在 `0029_follow_up_governance_idx`，不能用新隔离库验收代替已有业务数据迁移。

## 当前代码与兼容边界

| 范围 | 入口及保留要求 |
| --- | --- |
| 当前主线 | [Worker 装配](../src/opsswarm/worker/main.py)、[当前工厂](../src/opsswarm/worker/dynamic_factory.py)只接入当前 v15 冻结与 v16 现场动态图；复用[三轮路由图](../src/opsswarm/investigation/dynamic_third_round_routing_graph.py)，规划依赖[独立端口](../src/opsswarm/investigation/coordination/ports.py) |
| v1/v2 | 继续由同一 Worker 路由单 Agent 与固定多 Agent，保留[固定图工厂](../src/opsswarm/worker/task_graph.py)、版本化数据和回归 |
| v3 graph-v1—v14 | [历史工厂](../src/opsswarm/worker/dynamic_graph.py)共用冻结依赖装配，公开类名、各自图拓扑、版本门禁与恢复契约保留；不由当前 Worker 自动升级或领取。当前工厂的旧导入仍可用 |
| 业务事实与恢复 | [版本门禁](../src/opsswarm/investigation/workflow_versions.py)、[预算账本](../src/opsswarm/investigation/budget/store.py)、业务来源与[迁移](../alembic/versions)保持原契约；checkpoint 不代替事实或审计 |
| 处置与客观结果 | [执行 Worker](../src/opsswarm/execution/release_jobs/worker.py)、[恢复 Worker](../src/opsswarm/verification/candidate/worker.py)及现场回流仍需保留原授权、预算、租约、窗口与不可变历史 |
| 独立评测与历史复现 | 保留[评测入口](../src/opsswarm/evaluation/main.py)、[RCAEval RE2](../evaluations/rcaeval-re2)、历史 MVP 脚本与报告。手工命令集中于[参考页](manual-reference.md) |

## 后续工作

先围绕完整演示与复现收拢正式入口，避免依赖忽略目录中的临时脚本；把必要清理、验证和文档放在同一批完成，不追加角色、平台或按 142 顺延功能。

完整发布仍须在稳定环境补验正常恢复、事故收敛和槽位释放，并核查部署、保留卷、演示与发布材料。时钟作为独立环境欠项，不阻塞无需整栈的代码整改；下次恢复前核对凭据有效期、模型配置及 UTC 连续性。141 与本版之外的再次自动执行保持原边界，具体发布影响见[交付范围](architecture/local-multi-agent-release-scope.md)。

继续时阅读 [AGENTS](../AGENTS.md)、本文、[进度索引](progress/README.md)和本批相关代码；旧架构按依赖定向补读。当前必需项全部通过并完成演示与发布后结束本版，其余进入后续版本。
