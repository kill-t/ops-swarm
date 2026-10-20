# 第 103 步：版本化调查手册最小读取链路

## 1. 切片目标

让固定 v2 的专业 Agent 在一个明确事故场景中按需读取 Git 管理的调查手册，并能从持久账本说明它实际读取了哪个版本和内容散列。手册用于约束调查步骤，不作为现场证据、根因证明、审批或执行授权。

## 2. 设计与实现

- 新增 [第 103 步设计](../architecture/103-versioned-runbook-read.md)，固定 `runbook-read-v1` 上下文策略、`runbook-loop-input-v1` 输入版本和同名提示版本；旧 `task-context-v1`、`evidence-read-v1`、`summary-context-v1` 与历史输入保持原语义。
- 新增手册契约、注册表和读取器。首份 `order-service-release-correlation-v1` 由 JSON 元数据和 UTF-8/LF Markdown 正文组成，注册表复核不可变 ID、版本、选择器版本、8 KiB 大小上限和 SHA-256；正文材料 ID 同时绑定逻辑版本与内容散列，调用方不能提供路径。
- 首个确定性选择器只在 `order-service` 的变更任务快照中存在事故开始前一小时内的已完成部署时返回目录。它不读取标题、模型判断或冻结根因真值，数据库池与 Redis 场景不会命中。
- `read_runbook` 只接受 `runbook_id`、`version` 和 `content_sha256`。运行、任务、尝试、角色及允许目录由系统注入；日志、指标、变更分别使用独立账本操作并与原取证和 `read_evidence` 共享额度。
- 初始调用清单记录手册目录；读取成功后，下一次模型调用清单追加实际正文的稳定材料 ID、版本和散列。完整正文保存在配对工具结果中，恢复时从成功账本重建，不创建第二份业务表副本。
- 专业报告仍只能引用本尝试成功读取的现场证据。手册工具消息不会加入证据 ID 集合，也不能扩大工具、审批或执行权限。
- 固定 v2 图按策略装配手册循环；综合继续消费三份完整专业结果，不创建工作摘要。v3、经验和人工流程未接入。
- 迁移 `0060_versioned_runbook_read` 只扩展既有运行策略、模型输入和角色化工具操作约束。存在新策略、输入或手册读取事实时拒绝降级到 `0059`。

## 3. 状态、持久化与失败路径

手册正文由 Git 和 Python 包资源管理；运行策略、任务尝试、模型输入、上下文清单、工具请求、工具结果和预算结算继续使用既有 PostgreSQL 事实。一次读取必须绑定当前尝试同槽位的成功模型调用，参数必须属于该调用的确定性目录，工具结果必须与注册表正文、版本和散列一致。

未知 ID、版本或散列、目录外读取、跨任务/跨角色引用、路径字段、非法编码、内容漂移和超限正文都会失败关闭。同一专业任务的后续调用与技术重试复用首次持久化目录并与当前安装包逐项复核；若 Worker 发布错误地改写同一逻辑版本，恢复不会混合新目录和旧正文。失败或未配对的工具交互不会进入下一轮历史；技术重试使用新的任务尝试并保留运行累计预算。后续材料清单只有在读取成功后才包含正文引用，避免把“可见目录”误写成“已读正文”。

## 4. 验证

最终代码执行了以下验证：

- `uv run pytest tests/unit -q`：827 项通过，其中手册领域与加载器 19 项覆盖选择、编码、大小、散列和路径边界。
- `uv run pytest tests/api -q`：127 项通过，既有启动、调用输入、清单、交互和预算 API 保持兼容。
- `uv run pytest tests/integration/test_runbook_loop.py tests/integration/test_runbook_migration.py tests/integration/test_loop_migration.py tests/integration/test_evidence_loop.py tests/integration/test_loop_ledger.py tests/integration/test_context_reservations.py tests/integration/test_loop_graph.py tests/api/test_loop_api.py -q`：67 项通过。
- 上述真实 PostgreSQL 组覆盖 `read_runbook → read_evidence → SpecialistReport`、完整固定 v2 图、实际正文材料清单、共享预算、恢复重建、非法范围、旧策略回归、迁移升级和降级保护。
- `uv run ruff check src tests alembic`、`uv run ruff format --check src tests alembic` 与 `uv run mypy src` 通过；mypy 检查 308 个源文件。
- `uv run alembic heads`：唯一 head 为 `0060_versioned_runbook_read`。
- `uv build --wheel --out-dir .codex-build-103` 成功，wheel 同时包含手册 JSON 元数据与 Markdown 正文；临时构建目录已删除。
- `git diff --check` 通过，仅输出工作区既有 CRLF 转 LF 提示。

仓库根目录存在未跟踪的用户目录 `output/`，本片未读取、修改或提交。静态检查明确限定为 `src`、`tests` 和 `alembic`。

## 5. 限制与下一步

本片只有一份发布相关手册和一个确定性选择器，只接入固定 v2。手册尚未进入 v3 动态角色、协调、审查或规划上下文；没有搜索、经验混排、前端页面或公开下载 API，也没有证明 Token 或费用下降。

下一片先建立服务端可信操作者身份与最小权限模型，为经验候选的确认、拒绝、停用和替代提供可审计主体。经验审核不能依赖调用方自填 `actor_id`，也不能继承历史审批或获得执行权限。

## 6. 面试表达

> 我把调查手册作为 Git 管理的版本化材料接入了固定 v2 专业工具循环。系统先根据已持久化的服务、角色和近期部署事实确定目录，模型只能按 ID、版本和 SHA-256 读取，不能传文件路径。读取请求、完整结果和后续上下文清单都进入原有预算与交互账本，所以可以区分“目录中可见”和“模型实际读过”。报告引用校验仍只承认现场证据，手册不会变成根因证明或执行授权。新策略和迁移独立版本化，旧运行按原输入恢复。
