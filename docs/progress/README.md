# 开发进度文档规范

`docs/progress/` 用于记录每个可运行、可测试功能切片如何从设计走到验收。它不是聊天记录，也不是逐行修改日志。

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
