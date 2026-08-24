# 05 - 单 Agent 调查基线与离线评测

## 目标

这一切片为 `release_500` 故障建立第一个可重复的单 Agent 调查基线：Agent 能使用日志、指标和最近变更三个只读工具，返回经过 Pydantic 校验且引用真实证据的根因结论，并用冻结真值计算离线指标。

本切片不接控制面、Worker、业务数据库、LangGraph、多 Agent 或真实观测平台。真实模型是显式可选能力，自动化测试只使用冻结响应的假模型。

## 设计与边界

### LangChain 只位于 Agent 适配层

使用 LangChain 1.x 的 `create_agent` 复用标准工具调用循环，使用 `@tool` 暴露三个有界只读工具，使用 `ToolStrategy` 约束 `InvestigationConclusion`。Incident 状态机、评测真值、审批规则和未来执行逻辑没有放进提示词或 LangChain 内部状态。

### 数据源与框架解耦

`InvestigationDataSource` 定义日志、指标和变更三个只读端口。冻结场景实现该端口，未来真实适配器也实现同一协议。LangChain 工具只是薄适配层，既不保存业务事实，也不拥有写权限。

### 结构化输出后仍做事实校验

Pydantic 能验证字段、枚举、置信度和审批标记，却不能判断证据是否由工具返回。`EvidenceCollector` 记录每次真实工具调用和返回证据；运行结束后再做集合校验，引用未知证据会抛出 `UnverifiedEvidenceReference`。

### 固定场景先保存可比较基线

`release-500-baseline-v1` 冻结了同一组事故输入、日志、指标、变更和期望答案。当前先验证 Top-1、证据召回、建议准确率、安全违规率和工具调用数的计算链路；以后扩展场景数量并让单 Agent、多 Agent 共用。

## 实际改动

- 新增调查输入、证据、根因、建议、结构化结论和运行结果模型。
- 新增只读数据源协议、三个 LangChain 工具、有界 Pydantic 参数和线程安全的证据收集器。
- 新增固定 `release_500` 调查场景及其日志、指标和变更证据。
- 新增 `SingleAgentInvestigator`，完成工具循环、结构化输出和证据引用交叉校验。
- 新增确定性离线指标计算。
- 新增 `LLM_` 环境配置与 OpenAI 兼容模型工厂；缺少密钥或模型名时拒绝创建配置。
- 引入 LangChain 1.x 和 `langchain-openai` 1.x，并由 `uv.lock` 固定解析版本。
- 新增假模型工具循环、安全边界、模型校验、工具参数和配置测试。
- 新增单 Agent 架构事实文档，并同步 README、技术栈和进度索引。

## 测试方法与结果

定向测试和静态检查：

```powershell
uv run pytest tests/unit/investigation -q
uv run ruff check src/opsswarm/investigation tests/unit/investigation
uv run ruff format --check src/opsswarm/investigation tests/unit/investigation
uv run mypy
```

结果：9 个调查模块单元测试通过；Ruff 与 mypy 严格模式通过。测试覆盖三工具调用、结构化结论、根因与证据指标、回滚审批约束、未知证据拒绝、查询时间窗限制、未知服务拒绝和模型配置。

全仓库最终验收：

```powershell
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
docker compose config --quiet
git diff --check
```

结果：Ruff 与 mypy 严格模式通过，全仓库 92 个测试全部通过，其中 22 个测试通过 Testcontainers 使用真实 PostgreSQL 17；Compose 配置有效，Markdown 相对链接有效，Git 差异不存在空白错误。测试期间临时启动的 Docker Desktop 已在验收后关闭。

## 限制与下一步

- 当前只有一个冻结故障场景，不能据此声称真实模型具有稳定准确率。
- 假模型验证编排和规则，不代表模型推理质量；真实模型评测必须显式启用并记录模型版本、Token、成本和延迟。
- 调查运行只存在于当前调用，尚未关联 `Incident`、持久任务和 Worker，也不能在崩溃后恢复。
- 冻结数据源尚未接真实 Loki、Prometheus 或变更平台。
- 当前是通用 Agent loop，没有 LangGraph checkpoint、并行节点、证据质疑和失败循环。

下一切片将先持久化 `investigation_runs` 和 `work_items`，由独立 Worker 领取任务并调用现有单 Agent 基线。这样先解决任务不丢失、重试和运行记录，再进入多 Agent 编排。

## 面试表达

我没有一开始就堆多个 Agent，而是先用 LangChain 建立可测的单 Agent 基线。它只有日志、指标和变更三个只读工具，输入参数由 Pydantic 限制，结论也必须结构化。更关键的是，我不会把“结构化”误当成“真实”：系统会记录工具实际返回的证据 ID，拒绝模型引用不存在的证据。固定故障场景计算 Top-1、证据召回、安全违规率和工具调用数，为后续 LangGraph 多 Agent 提供同一基准。
