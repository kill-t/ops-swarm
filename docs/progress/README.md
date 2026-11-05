# 开发进度文档规范

`docs/progress/` 用于记录每个可运行、可测试功能切片如何从设计走到验收。它不是聊天记录，也不是逐行修改日志。

## 当前推进位置

更新日期：2026-11-05。每个切片完成后同步本节，继续开发前核对 Git 状态、相关实现和最新记录。

| 项目 | 当前状态与依据 |
| --- | --- |
| 已有运行交付 | 第 63 步 MVP 发布已验收，见 [发布记录](63-mvp-compose-release.md)；历史测试结果不是本轮重新执行的结果 |
| 最新文档决策 | 2026-11-05 [本地版本收口与核心优先](local-release-scope-decision.md)已完成文档验收；新增运行行为仍待实现，不占实现编号 |
| 最新实现切片 | 最小可信协作台通过组件与接口验收。见[协作台记录](trusted-collaboration-workroom.md)。141 完整 API 链欠项单列。见[实际验证及欠项](141-reinvestigation-approval-decision.md) |
| 当前架构来源 | 当前范围与终点以[本地多 Agent 交付设计](../architecture/local-multi-agent-release-scope.md)为准；[64长期路线](../architecture/64-full-multi-agent-roadmap.md)/[65系统设计](../architecture/65-agent-system-design.md)保留技术和安全规则 |
| 当前必需缺口 | 当前版本部署、浏览器现场联调、保留卷重启及发布演示 |
| 下一项完整行为 | 可复现部署与发布。优先复用现有 OpsSwarm 容器，核查业务卷迁移路径并完成发布场景 |
| 独立评测试点 | [第四批协作对照](rcaeval-re2-batch-04.md)通过预设门槛；下一方向为产品 Harness 中断恢复 |

状态约定：**已决定**表示方向确定但能力未实现；**开发中**表示代码或接口正在建设、尚未验收；**已验收**须有对应实现、独立提交和可复现验证记录。文档切片验收只代表文档完成，不将其中的目标架构标成已实现。旧记录保存当时事实与结论，后续以当前架构来源和新切片说明调整。

## 1. 文档粒度

- 架构初始化使用编号 `00`。
- 第一个可运行切片从 `01` 开始，之后按实施顺序递增。
- 一篇文档对应一个独立 Git 提交；小修复只有在形成独立可验证行为时才单独建文档。
- 范围与开发方式决策可以使用语义文件名，不占实现编号；实现编号用于索引历史，不决定后续目标或自动增加发布前置。按完整用户行为验收，必要实现提交可以分开。
- 实现切片文件名使用 `NN-short-description.md`，正文使用中文；独立范围/流程决策适用上述语义命名例外。
- 每片通常几百字：说明关键决定、改动、实际验证及下一步即可。以下问题可合并回答，简单切片不必逐节铺开；不以字数替代必要风险说明。
- 专项设计、进度记录、PROJECT_STATE各保留各自职责，用链接代替重复正文；交接只留当前事实、下一步和入口，历史验收留原记录。

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

“测试方法与结果”只记必要的复现命令、最终结论和重要未解风险。详细日志留在缓存报告，不重复统计历史总量、去重过程或细碎耗时；失败后复验只说明原因与最终状态。未执行的检查不得写为通过。验证范围遵循AGENTS的按风险策略，文档维护只检查链接和差异。

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
- [96 - v3 第二轮补查路由的第三轮计划持久化](96-third-round-planning.md)
- [97 - v3 第三轮专业任务执行与恢复](97-third-round-investigation.md)
- [98 - v3 第三轮综合与开放假设恢复](98-third-round-synthesis.md)
- [99 - v3 第三轮开放假设独立审查与恢复](99-third-round-review.md)
- [100 - v3 第三轮审查后的确定性最大轮次停止路由](100-third-round-terminal-routing.md)
- [101 - 只读 telemetry MCP 日志查询](101-telemetry-mcp-log-query.md)
- [102 - 只读 telemetry MCP 指标查询](102-telemetry-mcp-metric-query.md)
- [103 - 版本化调查手册最小读取链路](103-versioned-runbook-read.md)
- [104 - 可信操作者身份与最小权限](104-trusted-operator-identity.md)
- [105 - 经验候选契约、来源追踪与持久生成任务](105-experience-candidate-generation.md)
- [106 - 可信工程师经验审核、确认与拒绝](106-trusted-experience-review.md)
- [107 - 有效经验状态、停用与有界确定性检索](107-experience-lifecycle-search.md)
- [108 - 经验版本替代与不可变追溯](108-experience-replacement.md)
- [109 - 首轮协调的版本化经验上下文与引用追溯](109-coordination-experience-context.md)
- [110 - 有界来源核验与事务内复用](110-bounded-source-validation.md)
- [111 - v3 Worker 与终端路由持久交接](111-dynamic-worker-handoff.md)
- [112 - 可信人工补充与调查取消](112-trusted-human-wait-commands.md)
- [113 - 可信人工重评输入准备与就绪诊断](113-human-input-preparation.md)
- [114 - 可信人工输入实际重评与共享预算阶段归属](114-human-input-reassessment.md)

- [115 - 重评意见的确定性后续路由与持久交接](115-human-reassessment-routing.md)

- [116 - 候选评估意图的受限消费](116-candidate-evaluation.md)

- [117 - 候选评估结果的可信规划准入](117-candidate-plan-admission.md)

- [118 - 候选草案可信审批请求准入](118-candidate-approval-admission.md)

- [119 - 候选审批请求的可信决定与过期收敛](119-candidate-approval-decision.md)

- [120 - 候选受控执行准入与当前参数前置核验](120-candidate-execution-preflight.md)

- [121 - 版本化执行条件与可信核验事实](121-candidate-condition-verification.md)

- [122 - 新核验依据的独立可信审批](122-condition-basis-approval.md)

- [123 - 本地发布演练状态与版本CAS](123-lab-release-state-cas.md)

- [124 - 候选发布执行的可信准入与持久请求](124-candidate-release-execution-admission.md)

- [125 - 发布操作只读回执与确定性对账](125-release-receipt-reconciliation.md)

- [126 - 远端发布持久窗口与受限派发](126-bounded-release-dispatch.md)

- [127 - 候选发布可信任务与独立Worker](127-candidate-release-worker.md)

- [128 - 候选发布后的客观恢复验证](128-candidate-recovery-verification.md)

- [129 - 候选恢复结论与父运行持久收尾](129-candidate-parent-settlement.md)

- [130 - 候选处置结果与Incident受控收敛](130-candidate-incident-convergence.md)

- [131 - 验证失败的有界回流准入与原预算继承](131-bounded-reinvestigation-admission.md)

- [132 - 独立回流协调阶段与原账本首次调用](132-reinvestigation-coordination.md)

- [133 - 回流专业任务的确定性编译与持久分派](133-reinvestigation-dispatch.md)

- [134 - 独立回流专业执行与原账本调用](134-reinvestigation-execution.md)

- [135 - 独立回流综合与专业来源闭包](135-reinvestigation-synthesis.md)。

- [136 - 独立回流审查与精确提案绑定](136-reinvestigation-review.md)。

- [137 - 回流审查的确定性路由](137-reinvestigation-routing.md)。

- [138 - 回流候选评估意图的受限消费](138-reinvestigation-candidate.md)。

- [139 - 回流新候选的可信规划准入](139-reinvestigation-candidate-plan.md)。

- [140 - 回流新草案的可信审批请求准入](140-reinvestigation-approval-request.md)。

- [141 - 回流审批请求的可信决定与过期收敛](141-reinvestigation-approval-decision.md)（实现已提交，单元/迁移通过，完整API链未验收）。
