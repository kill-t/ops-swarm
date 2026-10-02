# 开发进度文档规范

`docs/progress/` 用于记录每个可运行、可测试功能切片如何从设计走到验收。它不是聊天记录，也不是逐行修改日志。

## 当前推进位置

更新日期：2026-10-02。每个切片完成后同步本节，继续开发前核对 Git 状态、相关实现和最新记录。

| 项目 | 当前状态与依据 |
| --- | --- |
| 已有运行交付 | 第 63 步 MVP 发布已验收，见 [发布记录](63-mvp-compose-release.md)；历史测试结果不是本轮重新执行的结果 |
| 最新文档切片 | 第 78 步开发交接已验收，见 [切片记录](78-development-handoff.md)；以 [项目状态](../PROJECT_STATE.md) 为新会话入口，原第 65 篇仍为架构事实来源 |
| 最新实现切片 | 第 95 步 v3 第二轮独立审查的确定性再次路由已验收，见 [切片记录](95-second-round-routing.md)；图 v10 按轮保存两条路由事实，通用 Worker 仍跳过 v3 |
| 当前架构来源 | [第 64 篇总路线](../architecture/64-full-multi-agent-roadmap.md) + [第 65 篇专项设计](../architecture/65-agent-system-design.md) |
| 待开发能力 | 完整 Harness、专业交互历史裁剪、动态协作、MCP、经验、图内人工介入和多步骤处置 |
| 下一切片 | 第 96 步：把第二轮 `follow_up` 路由确定性编译为第三轮计划与待执行任务，不提前执行第三轮、人工暂停或规划 |

状态约定：**已决定**表示方向确定但能力未实现；**开发中**表示代码或接口正在建设、尚未验收；**已验收**须有对应实现、独立提交和可复现验证记录。文档切片验收只代表文档完成，不将其中的目标架构标成已实现。旧记录保存当时事实与结论，后续以当前架构来源和新切片说明调整。

## 1. 文档粒度

- 架构初始化使用编号 `00`。
- 第一个可运行切片从 `01` 开始，之后按实施顺序递增。
- 一篇文档对应一个独立 Git 提交；小修复只有在形成独立可验证行为时才单独建文档。
- 文件名使用 `NN-short-description.md`，正文使用中文。

## 2. 每篇文档必须回答

1. 这一步解决了什么业务或工程问题？
2. 为什么采用当前设计，边界在哪里？
3. 实际增加或修改了什么？
4. 如何运行和测试，实际结果是什么？
5. 有哪些限制、风险或下一步？
6. 面试时怎样用一两分钟讲清楚？

## 3. 模板

```markdown
# NN - 切片名称

## 目标

## 设计与边界

## 实际改动

## 测试方法与结果

## 限制与下一步

## 面试表达
```

“测试方法与结果”必须记录真实执行的命令和结果，不得在测试前填写“已通过”。如果某项测试因环境阻塞，要明确记录阻塞原因和未验证风险。

## 4. 当前记录

- [00 - 项目章程与架构设计](00-project-charter-and-architecture.md)
- [01 - 确定性版本 500 故障模拟](01-deterministic-release-500-fault.md)
- [02 - Incident 纯领域状态机](02-incident-domain-state-machine.md)
- [03 - PostgreSQL 事故持久化](03-postgresql-incident-persistence.md)
- [04 - FastAPI 事故控制面](04-fastapi-incident-control-plane.md)
- [05 - 单 Agent 调查基线与离线评测](05-single-agent-investigation-baseline.md)
- [06 - 持久调查任务与独立 Worker](06-persistent-investigation-worker.md)
- [07 - LangGraph 并行调查基线](07-langgraph-parallel-baseline.md)
- [08 - LangGraph PostgreSQL checkpoint 恢复](08-postgresql-checkpoint-recovery.md)
- [09 - 多 Agent 持久 Worker 路由](09-multi-agent-worker-routing.md)
- [10 - Evidence Board 业务持久化](10-evidence-board.md)
- [11 - 根因假设与确定性质疑](11-hypothesis-challenge.md)
- [12 - 版本化处置计划与风险分级](12-action-plan-and-risk-policy.md)
- [13 - 精确版本审批请求与原子状态迁移](13-approval-request.md)
- [14 - 审批决定、过期收敛与审计](14-approval-decision.md)
- [15 - 确定性白名单执行器与幂等操作](15-deterministic-operation-executor.md)
- [16 - 逐次恢复验证与持久样本](16-persistent-recovery-verification.md)
- [17 - 可重放事故时间线 SSE](17-replayable-sse-timeline.md)
- [18 - Incident 关闭与告警复发](18-incident-closure-and-recurrence.md)
- [19 - Incident 作战室与实时事件展示](19-incident-workroom-ui.md)
- [20 - Incident 作战室处置链路聚合展示](20-incident-workroom-projection.md)
- [21 - 三故障版本化调查评测集](21-versioned-investigation-evaluation.md)
- [22 - 评测结果持久化与只读查询](22-evaluation-persistence-and-query.md)
- [23 - 只读评测面板](23-evaluation-dashboard-ui.md)
- [24 - 评测实验身份、重复运行与遥测](24-evaluation-experiments-and-telemetry.md)
- [25 - 真实模型单场景冒烟与尝试生命周期](25-real-model-smoke-attempts.md)
- [26 - DeepSeek 真实模型兼容与首次对照](26-deepseek-real-model-smoke.md)
- [27 - 真实模型三场景批量评测](27-real-model-batch-evaluation.md)
- [28 - 处置建议契约与评测集 v2](28-recommendation-contract-v2.md)
- [29 - 确定性处置计划能力映射](29-action-plan-capability-mapping.md)
- [30 - 数据库连接池配置回滚计划模板](30-database-pool-configuration-plan.md)
- [31 - 数据库连接池确定性故障演练](31-database-pool-fault-lab.md)
- [32 - 配置回滚受控执行与恢复验证](32-configuration-execution-and-verification.md)
- [33 - 持久恢复验证调度 Worker](33-persistent-verification-scheduler.md)
- [34 - Redis 不可用确定性处置闭环](34-redis-dependency-recovery.md)
- [35 - 真实 Redis 与受控故障代理](35-real-redis-fault-proxy.md)
- [36 - Prometheus 指标与恢复验证](36-prometheus-recovery-verification.md)
- [37 - Prometheus 只读调查数据源](37-prometheus-investigation-data-source.md)
- [38 - 结构化日志与 Loki 只读调查数据源](38-structured-logs-and-loki-investigation.md)
- [39 - 持久变更记录模拟器与只读调查数据源](39-change-record-simulator-and-investigation.md)
- [40 - 统一现场数据源与调查 Worker 安全切换](40-unified-live-investigation-worker.md)
- [41 - OpenTelemetry 跨进程调查追踪](41-opentelemetry-investigation-tracing.md)
- [42 - 冻结与现场常驻调查工作池](42-persistent-investigation-worker-pools.md)
- [43 - 调查尝试审计与模型成本](43-investigation-attempt-audit.md)
- [44 - 作战室调查尝试与成本展示](44-investigation-attempt-workroom.md)
- [45 - GitHub 只读变更调查数据源](45-github-readonly-change-source.md)
- [46 - 部署版本与 GitHub Commit 确定性关联](46-deployment-commit-provenance.md)
- [47 - 作战室展示部署代码来源](47-deployment-provenance-workroom.md)
- [48 - 受限 CI/CD 部署事件摄取](48-cicd-deployment-event-ingestion.md)
- [49 - 部署构建产物摘要](49-deployment-artifact-digest.md)
- [50 - 运行实例构建产物核对](50-runtime-artifact-verification.md)
- [51 - 多实例运行清单与滚动发布判定](51-runtime-inventory-rollout.md)
- [52 - Ed25519 签名运行清单证明](52-signed-runtime-inventory-proof.md)
- [53 - 独立部署控制器与动态证明刷新](53-deployment-controller-runtime-proof-refresh.md)
- [54 - 本地审批中心与待办队列](54-approval-center-ui.md)
- [55 - 已批准计划的受控执行中心](55-controlled-execution-ui.md)
- [56 - 恢复验证中心与采样进度展示](56-recovery-verification-center.md)
- [57 - 已恢复 Incident 的人工关闭入口](57-incident-closure-ui.md)
- [58 - 确定性事故复盘报告快照](58-persistent-incident-report.md)
- [59 - 事故复盘报告生成、查看与打印](59-incident-report-ui.md)
- [60 - 复盘改进行动项与完成审计](60-report-action-items.md)
- [61 - 复盘改进行动项前端闭环](61-report-action-items-ui.md)
- [62 - 跨事故改进行动项治理中心](62-follow-up-governance-center.md)
- [63 - MVP 一键 Compose 交付与发布验收](63-mvp-compose-release.md)
- [64 - 完整多 Agent 工程版目标、基础约束与开发路线](64-full-multi-agent-roadmap.md)
- [65 - Agent 系统最终设计与事实核对约定](65-agent-system-design.md)
- [66 - 调查轮次、专业任务与尝试持久化](66-investigation-task-persistence.md)
- [67 - 固定图接入专业任务、结果与进度事件](67-fixed-graph-task-integration.md)
- [68 - 运行级共享调用预算与预留账本](68-shared-call-budget.md)
- [69 - 模型输入、Token 与估算费用预算](69-model-input-budget.md)
- [70 - 受限异步取证与迟到结果隔离](70-bounded-async-evidence.md)
- [71 - 模型调用前的专业取证快照与恢复复用](71-task-evidence-snapshot.md)
- [72 - 现场来源的显式观测窗口](72-explicit-observation-windows.md)
- [73 - 现场 v2 持久任务取证](73-live-task-evidence.md)
- [74 - 冻结场景身份的确定性与历史兼容](74-stable-frozen-identity.md)
- [75 - 任务上下文组装与材料清单](75-task-context-builder.md)
- [76 - 上下文证据目录与受控详情读取](76-context-evidence-reader.md)
- [77 - 同一尝试内的重复调用身份](77-repeated-call-identity.md)
- [78 - 开发交接与项目状态入口](78-development-handoff.md)
- [79 - 专业 Agent 有界按需证据读取](79-bounded-evidence-reading.md)
- [80 - 已完成专业任务的结构化工作摘要](80-task-working-summary.md)
- [81 - 工作摘要进入版本化上下文](81-summary-context.md)
- [82 - 动态调查计划与确定性分派校验](82-dynamic-coordination-policy.md)
- [83 - 动态协调计划持久化](83-dynamic-coordination-persistence.md)
- [84 - 协调模型结构化调用与共享预算账本](84-coordination-model-ledger.md)
- [85 - 独立动态工作流版本与运行策略冻结](85-dynamic-workflow-start.md)
- [86 - v3 动态图首轮协调与 checkpoint 恢复](86-dynamic-coordination-graph.md)
- [87 - v3 动态首轮任务分派与专业结果恢复](87-dynamic-first-round-fanout.md)
- [88 - v3 首轮协调综合与开放假设持久化](88-dynamic-first-round-synthesis.md)
- [89 - v3 独立假设审查与审查事实持久化](89-independent-hypothesis-review.md)
- [90 - v3 确定性审查路由与路由事实持久化](90-deterministic-review-routing.md)
- [91 - v3 确定性补查计划与第二轮任务持久化](91-deterministic-follow-up-planning.md)
- [92 - v3 第二轮专业任务执行与恢复](92-second-round-investigation.md)
- [93 - v3 第二轮综合与开放假设恢复](93-second-round-synthesis.md)
- [94 - v3 第二轮开放假设独立审查与恢复](94-second-round-review.md)
- [95 - v3 第二轮独立审查的确定性再次路由](95-second-round-routing.md)
