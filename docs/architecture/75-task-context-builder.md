# 75 - 任务上下文组装与调用材料清单

## 目标与边界

在固定 v2 图中接入最小 ContextBuilder，将专业任务、取证快照和已提交报告组装为可追溯的模型输入。新建且显式配置模型预算的 v2 运行固定 `context_policy_version=task-context-v1`；同时固定提示版本 `fixed-task-context-v1`；历史运行保持空值，沿用原提示与预算输入，不回填或静默升级 checkpoint。

本片保留完整且有界的当前证据，不实现按需工具读取、历史交互压缩、经验检索、动态分派或人工补充。当前模型没有自主业务工具，材料清单的工具集合为空；结构化输出 Schema 单独随实际输入记录。

## 状态、输入与职责

- `ContextBuilder` 是确定性组装组件，接收已校验材料，生成 `ContextPacket`（实际 ModelInput、清单和预算估算），不访问数据库或模型。
- 读取组件从业务库装载同一运行的任务分派、取证快照及专业结果，专业调用只读自己的任务；综合调用只接受三个已成功提交的专业结果。
- 专业输入显式包含任务目标、服务、时间窗、证据范围和完成要求。综合输入包含分派目录、证据和报告，不传递其他角色聊天记录。
- 图传入的事故、证据和报告必须与业务材料一致；重复证据、跨事故、跨专业、缺失材料和冲突报告拒绝组装，不静默替换错误输入。
- 沿用不可信证据指令；工具文本仅作为 user 消息中的 JSON 数据。单次输入不能扩张权限，模型仍只能返回结构化建议。

```mermaid
flowchart LR
    graph[固定图输入] --> load[读取当前任务材料]
    load --> build[ContextBuilder 组装与估算]
    build --> reserve[租约事务内复核材料]
    reserve --> commit[保存输入、清单和预算预留]
    commit --> model[LangChain 模型调用]
    model --> usage[结算用量和保存结果]
```

## 持久化与恢复

迁移 `0036_task_context_manifest` 新增运行上下文策略列和调用账本 `context_manifest` JSONB。清单记录运行/任务、角色及策略版本、材料类型/ID/版本/内容散列/证据引用、工具集合、裁剪记录和实际输入散列/预算估算。事故输入以本次输入内的不可变副本为审计来源；任务、证据及结果使用已有只读查询入口定位。裁剪记录本片为空，表示未进行截断或压缩。

模型请求前先组装并估算；在已有父运行锁和租约事务内重新核对材料、清单及实际输入，连同预算预留一起提交。任何校验或预算失败均不发出模型请求。并发共享预算、重试累计及未知用量语义沿用既有实现。提交预留后崩溃仍保留输入与清单；重试产生新的调用记录，不能覆盖旧清单。同一幂等预留更改材料清单必须冲突。

本次 ModelInput 的历史 JSON 格式及散列算法不变。当前角色和输出契约由 fixed-roles-v1 与保存的完整 Schema 对应；后续修改角色、契约或组装算法时需保留旧策略实现或引入新版本，不能沿用 task-context-v1 静默恢复旧运行。空策略不允许写入新清单；启用策略的模型调用必须有清单。降级遇到已固定策略或已保存清单时拒绝删除审计事实。

### 材料定位

| 材料类型 | ID 与版本 | 内容散列和读取来源 |
| --- | --- | --- |
| incident_input | Incident ID / incident-input-v1 | 本次 ModelInput 中 incident 的规范 JSON；不能事后改读当前 Incident 来替代当时输入 |
| task_assignment | Task ID / task-assignment-v1 | 完整 assignment 的规范 JSON，通过运行 collaboration 查询读取 |
| task_evidence | Task ID / 运行取证策略版本 | 已保存 TaskEvidenceSnapshot 的原散列，通过任务 evidence 接口读取；原采集尝试与成功工具在该记录中追溯 |
| task_result | Task ID / specialist-result-v1 | 已提交 SpecialistTaskResult 的规范 JSON，通过任务 result 接口读取 |

专业清单包含事故输入、分派和取证三项；综合清单包含事故输入和三个任务的分派、证据及结果，共十项。排序只规范化输入顺序，不丢弃证据，不把专业报告中的不确定性改写为事实。尚未采集的证据与已提交空证据严格区分，空快照可作为真实的查询结果进入上下文。

固定图的结构化输入先通过 TaskContextInput 校验；任务状态或材料不完整用稳定 context 错误拒绝调用。预算超限沿用 model_input_budget_exceeded 等错误，由既有任务/Worker 失败路径记录。API 返回的清单只用于审计，不能赋予证据读取范围、工具权限或执行审批。

## API

- InvestigationRunResponse 增加服务器固定的 `context_policy_version`，不增加可由模型或 API 调用方修改策略的字段。
- 新增 `GET /api/v1/investigations/{run_id}/calls/{call_id}/context-manifest`：返回清单；历史调用和工具调用返回 null；事故范围不匹配或调用不存在返回 404。
- 原 model-input、任务、证据、结果及预算查询保持兼容，预算列表不携带大块上下文清单。

## 验收

以假 SDK 传输和真实 PostgreSQL 验证任务隔离、两个运行并发不串用、综合报告前置、输入和引用完整性、恶意文本不改变系统消息、超限前停止、请求前清单已提交、幂等冲突、重试材料追踪、旧策略兼容、只读 API 与迁移护栏。各切片持续执行 Ruff、mypy 和文档链接检查。

后续切片再增加证据目录与按需读取，然后增加结构化工作摘要和动态调查。
