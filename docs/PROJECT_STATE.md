# OpsSwarm 项目交接

更新于 2026-11-13。当前目标是整改并收口可演示、可恢复的本地多 Agent 版本。交付边界以[本地版本范围](architecture/local-multi-agent-release-scope.md)为准，历史验收记录保留原有含义。

## 当前状态

- 仓库固定为 `D:\PyCharm\job-project\ops-swarm`，使用仓库 `.venv/Scripts/`。迁移 head 为 `0100_reflow_input_projection`，继续前核对 Git。
- 用户已允许按需启动服务。Docker Desktop 已启动，`opsswarm-local-demo` 和 `opsswarm-local-release` 均已停止。最新 35 秒 Linux 探针捕获两次超过 100 毫秒的墙钟偏差。隔离演示数据库通过保留卷重启核对，卷内还没有事故或 checkpoint，完整案例读回待验收。[发布环境预检](progress/local-demo-release-precheck.md)
- 当前整改已完成现行图与历史装配分离、历史工厂去重、首页收口、正式演示入口及[控制面错误转换整理](progress/control-plane-error-boundary.md)。装配历史见[整理记录](progress/legacy-assembly-and-project-entry-cleanup.md)。正式入口的配置、启动与阻断路径通过；三个完整场景仍待稳定环境验收。按完整问题推进，避免将内部操作拆成多轮交付。
- 协作台提交期间的状态更新和后台步骤补读已通过组件回归。历史浏览器超时案例及完整现场操作仍待复验。[协作台刷新记录](progress/collaboration-panel-refresh.md)
- 正式演示入口的令牌格式、启动调查字段及人工续接来源已对齐真实接口。四类角色的 ASGI 自查与命令契约通过离线验证，报告已纳入持久交接和续接的对象身份。[演示命令契约记录](progress/local-demo-command-contract.md)
- `handoff` 使用冻结 v15 完成两轮补查并续接至候选。隔离 PostgreSQL 验证 API 与 Worker 实例替换后的账本对账和实际续接。Compose 容器重启和完整发布场景仍待稳定环境复验。[冻结演示记录](progress/local-demo-frozen-handoff.md)
- 正式恢复脚本已在隔离 Linux、进程内 API 和真实 PostgreSQL 验证到事故收敛与槽位释放，包含原执行租约到期后的只读接手。上次容器预检仍捕获 UTC 倒退，正式 Compose 待稳定环境复验。[恢复验收](progress/local-demo-recovery-proof.md) [核验时间与环境记录](progress/local-demo-approval-time.md)
- 正式失败回流脚本已用 HTTP 契约测试验证唯一准入审计、新窗口证据、独立复查和持久接管的核对逻辑。报告保留重启读回所需的对象身份。使用当前入口的整栈运行仍待验收。[失败回流验收断言](progress/local-demo-reflow-evidence.md)
- 用户未跟踪目录 `output/`、`tmp/` 未读取、修改或提交；不访问原 RAG 仓库。已有 OpsSwarm 容器满足条件时优先复用，需要写入的验收使用隔离数据库。

## 能力与验收边界

| 能力 | 已确认事实与来源 |
| --- | --- |
| 现场动态调查 | graph-v16 已接入动态专业分工、精确窗口取证、摘要、共享预算、首轮可信经验与终端交接。真实 PostgreSQL 和 checkpoint 验证使用脚本模型及模拟 MCP；见[现场协作](progress/live-dynamic-investigation.md) |
| 人工续接与单步骤处置 | 可信补充、实际重评、候选、核验、审批、唯一 CAS、客观采样及收敛已有真实 PG 和进程内演练代表链。见[人工续接](progress/live-human-continuation.md) |
| 一次失败回流 | 现场新窗口取证、综合、独立复查及持久人工接管已在隔离 Compose 代表场景通过；原预算、账本、槽位和唯一操作保持。见[输入投影与整栈验证](progress/reflow-input-projection.md) |
| 协作台与部署 | 最小可信协作台通过组件/API 验收，隔离 Compose 通过新库部署、保留卷读取、浏览器补充准备及指定跨容器续接。见[协作台](progress/trusted-collaboration-workroom.md)、[启动](progress/local-compose-smoke.md)、[续接与中断](progress/local-compose-continuation.md)、[发布入口](progress/local-release-entry.md) |
| 正常发布完整行为 | 正式脚本的单次 CAS、中断后只读接手、三次业务采样、事故收敛和槽位释放已通过隔离 PostgreSQL 集成验收。正式 Compose 的完整场景待补验。见[恢复验收](progress/local-demo-recovery-proof.md)、[来源遍历](progress/local-release-source-traversal.md)、[状态快照](progress/local-release-status-snapshot.md)、[采样等待](progress/local-recovery-sample-wait.md) |
| 环境与其他欠项 | WSL 重置及授权停服务对照未消除 UTC 倒退，服务已恢复原状；见[校时核查](progress/local-wsl-clock-source.md)。141 单元与迁移通过、完整 API 未验收，不能把其提前拒绝全部归因于时钟；见[原记录](progress/141-reinvestigation-approval-decision.md) |

以上组件或指定场景成功均不代表当前完整版本已发布，不将脚本模型结果写成真实模型推理效果。已有业务卷曾停在 `0029_follow_up_governance_idx`，不能用新隔离库验收代替已有业务数据迁移。

## 当前代码与兼容边界

| 范围 | 入口及保留要求 |
| --- | --- |
| 控制面协议 | [应用工厂](../src/opsswarm/control_plane/app.py)装配资源、路由及响应元数据；[错误转换](../src/opsswarm/control_plane/error_handlers.py)统一注册与序列化，历史错误状态/字段、401 挑战与校验信息边界保持 |
| 当前主线 | [Worker 装配](../src/opsswarm/worker/main.py)、[当前工厂](../src/opsswarm/worker/dynamic_factory.py)只接入当前 v15 冻结与 v16 现场动态图；复用[三轮路由图](../src/opsswarm/investigation/dynamic_third_round_routing_graph.py)，规划依赖[独立端口](../src/opsswarm/investigation/coordination/ports.py) |
| v1/v2 | 继续由同一 Worker 路由单 Agent 与固定多 Agent，保留[固定图工厂](../src/opsswarm/worker/task_graph.py)、版本化数据和回归 |
| v3 graph-v1—v14 | [历史工厂](../src/opsswarm/worker/dynamic_graph.py)共用冻结依赖装配，公开类名、各自图拓扑、版本门禁与恢复契约保留；不由当前 Worker 自动升级或领取。当前工厂的旧导入仍可用 |
| 业务事实与恢复 | [版本门禁](../src/opsswarm/investigation/workflow_versions.py)、[预算账本](../src/opsswarm/investigation/budget/store.py)、业务来源与[迁移](../alembic/versions)保持原契约；checkpoint 不代替事实或审计 |
| 处置与客观结果 | [执行 Worker](../src/opsswarm/execution/release_jobs/worker.py)、[恢复 Worker](../src/opsswarm/verification/candidate/worker.py)及现场回流仍需保留原授权、预算、租约、窗口与不可变历史 |
| 独立评测与历史复现 | 保留[评测入口](../src/opsswarm/evaluation/main.py)、[RCAEval RE2](../evaluations/rcaeval-re2)、历史 MVP 脚本与报告。手工命令集中于[参考页](manual-reference.md) |

## 后续工作

正式复现统一使用[演示手册](local-demo.md)中的 `scripts/local-demo.ps1`，不依赖旧缓存案例。它使用独立卷与脚本模型，默认配置位于忽略目录 `.pytest_cache/local-demo`，身份有效八小时；过期使用新子目录准备并启动。稳定环境下顺序补验 handoff、recovery、reflow，不追加角色、平台或按 142 顺延功能。

完整发布仍须在稳定环境用 Compose 补验三个场景，并核查部署、保留卷、演示与发布材料。时钟作为独立环境欠项，下次恢复前核对凭据有效期、模型配置及 UTC 连续性。141 与本版之外的再次自动执行保持原边界，具体发布影响见[交付范围](architecture/local-multi-agent-release-scope.md)。

继续时阅读 [AGENTS](../AGENTS.md)、本文、[进度索引](progress/README.md)和本批相关代码；旧架构按依赖定向补读。当前必需项全部通过并完成演示与发布后结束本版，其余进入后续版本。
