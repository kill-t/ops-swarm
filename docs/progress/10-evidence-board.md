# 10 - Evidence Board 业务持久化

## 目标

把单 Agent 和多 Agent 调查实际取得的证据保存为可审查业务事实，并持久化多 Agent 专业报告与证据引用。Worker 重试不能留下重复证据或半成品板，API 不能把 checkpoint 中间状态伪装成已提交事实。

本切片不实现根因假设、支持/反驳关系、质疑节点、处置计划、审批或真实遥测接入。

## 设计与边界

- `InvestigationArtifacts` 在数据库事务前校验结果、证据和报告引用一致性。
- 单 Agent 收集器除了统计证据 ID，还保留工具实际返回的规范化 `EvidenceObservation`。
- 多 Agent 图在最终结果中返回排序后的证据和三份专业报告。
- 业务库新增 `evidence_items`、`specialist_reports` 和 `specialist_report_evidence`。
- 关联表使用包含 `investigation_run_id` 的复合外键，数据库层拒绝跨运行引用。
- Evidence Board 与 `run=succeeded`、`work_item=completed` 在有效租约保护下同事务提交。
- API 只读取业务库；排队或重试时返回空板，不读取 checkpoint pending writes。

## 实际改动

1. 增加 Alembic `0004_evidence_board`，创建三张 Evidence Board 表、检查约束、唯一约束、索引和复合外键。
2. 扩展证据收集器，使重复相同证据可以归一化，重复标识但内容冲突会被拒绝。
3. 单 Agent 和 LangGraph 多 Agent 统一向 Worker 返回完整证据材料；多 Agent 同时返回专业报告。
4. `InvestigationStore.complete` 在确认当前租约后原子写入运行结果、任务终态和 Evidence Board。
5. 证据数据库 ID 使用稳定 UUID5，规范化内容保存 SHA-256 摘要。
6. 增加 `GET /api/v1/investigations/{run_id}/evidence-board` 读取接口和 OpenAPI 契约。
7. 更新系统架构、技术栈、数据库、API 和前序 Worker 路由文档。

## 测试方法与结果

定向测试覆盖：

```powershell
uv run pytest tests/unit/investigation tests/api/test_control_plane_openapi.py tests/api/test_control_plane_api.py tests/integration/test_investigation_store.py tests/integration/test_multi_agent_worker.py tests/integration/test_migrations.py -q
```

结果：54 个切片相关测试通过。覆盖材料集合一致性、重复返回标识拒绝、超大证据属性拒绝、跨专业和跨调查引用拒绝、单 Agent 证据落库、API 完整板响应、Alembic 回退升级，以及第一个多 Agent Worker 失败时业务板为空、第二个 Worker 从 checkpoint 恢复后一次性提交三类证据和三份报告。

完整验收：

```powershell
uv run pytest
uv run pytest --collect-only -q -m integration
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run alembic check
docker compose config --quiet
git diff --check
```

结果：全量 127 个测试通过，其中收集到 38 个真实 PostgreSQL 集成测试；102 个 Python 文件格式检查通过，Ruff、mypy 和 Alembic 元数据一致性检查通过，Docker Compose 配置及 Git 空白错误检查通过。本地持久业务库从 `0003_multi_agent_worker` 成功升级到 `0004_evidence_board (head)`，`ops-swarm-control-db-1` 保持健康运行。

## 限制与下一步

- `source` 当前只是冻结数据源名称，尚未保存真实平台查询 URI、时间窗和工具版本。
- SHA-256 用于检测持久化内容漂移，不是外部来源的数字签名。
- 最终结论的证据引用仍在结构化结果内，没有独立根因假设及支持/反驳关系。
- Evidence Board 不提供修改或删除 API，后续归档需要单独保留策略。

下一切片增加根因假设、证据支持/反驳关系和确定性质疑节点。质疑结果必须持久化，证据不足时回到调查，而不是提高置信度后直接进入处置。

## 面试表达

我把 Agent 输出拆成运行摘要和规范化 Evidence Board。Worker 只有在租约有效时，才能在同一事务中提交任务完成、调查成功、证据、专业报告和引用。应用层校验证据集合及专业类型，数据库再用复合外键阻止跨调查引用。LangGraph 失败时中间输出只保存在 checkpoint，恢复成功后才成为可供工程师审查的业务事实。
