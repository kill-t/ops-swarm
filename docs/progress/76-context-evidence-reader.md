# 76 - 上下文证据目录与受控详情读取

## 目标

让工程师按一次模型调用查看证据摘要目录，需要时再读取一条完整观察；保留任务、来源和快照版本，避免从事故所有材料中误取当时没有引用的证据。

## 设计与边界

先更新 [专项设计](../architecture/76-context-evidence-reader.md)、数据库和 API 入口。目录使用已有材料中的完整摘要，不经模型压缩、不隐藏反证；较大的 attributes 只随单条详情返回。应用服务在构造时绑定运行和调用，读取参数仅选择该调用范围内的任务、证据和散列。

当前 API 用于本地控制面的事后复核，调用清单不是身份认证或执行授权。此次未修改模型输入、提示、策略和预算，也未新增模型工具；模型自主读取需后续工具循环接入，并由系统注入调用范围。

## 实际改动

- evidence_contracts 保存有界目录、组、条目和详情契约；目录最多三个专业组，每组最多二十条，完整保留摘要、来源、时间及窗口。
- ContextEvidenceReader 组织调用清单与已有快照的读取校验，检查任务归属、角色、版本、内容散列和证据 ID。空快照与无上下文清单保持不同响应。
- 新增 evidence-catalog 和带 snapshot_sha256 的单条 evidence GET 接口；复用既有查询依赖，不新增连接池、SQL 查询协议或数据库迁移。
- 越界任务和未知证据返回 404；陈旧引用、缺失或损坏材料明确冲突，不切换版本、不回退重新取证、不返回部分目录。
- 每次读取只返回已有事实，不修改清单、任务、预算或审批。新接口可以在调用预留后或最终完成后读取，不依赖活动 Worker 或 checkpoint。

## 测试方法与结果

验收日期 2026-09-08。统一回归 **393 passed，耗时 120.21 秒**。单元测试使用固定材料，API 与并发图使用真实 PostgreSQL；现场路径使用真实适配器和假 HTTP 响应，不代表真实遥测现场验收。

```powershell
.venv/Scripts/python.exe -u -m pytest tests/unit/investigation tests/integration/test_context_reservations.py tests/integration/test_context_graph.py tests/integration/test_model_budget_graph.py tests/integration/test_fixed_task_graph.py tests/integration/test_task_evidence_store.py tests/integration/test_evidence_snapshot_recovery.py tests/integration/test_live_task_graph.py tests/api/test_context_evidence_api.py tests/api/test_context_manifest_api.py tests/api/test_model_budget_api.py tests/api/test_call_budget_api.py tests/api/test_collaboration_api.py tests/api/test_task_evidence_api.py --tb=short
.venv/Scripts/ruff.exe check .
.venv/Scripts/ruff.exe format --check .
.venv/Scripts/mypy.exe src
git diff --check
```

Ruff、541 个文件的格式检查、mypy（217 个源文件）通过；9 篇 Markdown 结构、263 个本地链接与 `git diff --check` 检查通过。

新增 15 项测试覆盖完整摘要与大属性分离、恶意文本只作为数据、空快照、无清单、越界任务/证据、陈旧散列、缺失材料、散列损坏、重新计算散列后的内容替换、重复引用和只读 API。既有两个事故并发图测试增加专业及综合目录/详情与实际模型输入的逐条比对，完成运行后仍可读取；冻结和假传输现场两条路径都参与验证。

首次 PostgreSQL 测试因 Docker 未启动而没有执行，启动本地 Docker 后重新验证。没有改用 SQLite、真实模型或生产环境。

## 限制与下一步

- 目录虽然不返回大属性，服务内部仍加载最多三个完整有界快照完成校验；当前规模不新增索引或物化目录。
- 模型仍按第 75 步策略接收完整证据，本片没有减少模型 Token，也没有启用 Agent read_evidence 工具。
- 下一片接入有界模型按需读取与新上下文策略，包含共享预算、工具消息配对和逐次材料清单；之后再推进结构化工作摘要及动态调查。

## 面试表达

> 我把模型当时看到的证据整理成可追溯目录，目录保留完整摘要，详情必须使用任务 ID、证据 ID 和快照散列读取。读取范围绑定到那次调用，不能从另一个专业或事故捞材料。数据变更或引用过期时明确失败，而不是悄悄拿最新内容替换。目录、详情和模型输入在真实数据库测试中逐一核对，为后续 Agent 按需读取建立稳定的业务边界。
