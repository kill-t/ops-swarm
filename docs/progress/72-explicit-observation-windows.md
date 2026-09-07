# 72 - 现场来源的显式观测窗口

## 目标

让现场查询明确回答“调查哪段时间”，避免工具调用、技术重试或响应生成时间推进后，实际查询范围悄悄变化。历史发布事件与当前运行实例分开解释，为现场 v2 接入第 71 步取证快照提供来源侧能力。

## 设计与边界

先保存 [专项设计](../architecture/72-explicit-observation-windows.md)，同步数据库与 API 事实来源。本片增加来源侧显式查询入口与独立变更服务只读 API，不改变现有任务策略或数据库结构；现场 v2 尚未开放。

使用秒级、带时区的 UTC 闭区间，拒绝隐式时区、微秒舍入、未来截止点和过长窗口。日志、指标支持 1～60 个完整分钟，变更最长 24 小时。窗口由调用方明确传入，当前时钟只参与请求合法性及响应新鲜度检查。

变更窗口同时限制 `occurred_at` 和 `recorded_at`，返回原窗口与本次 `generated_at`。后入库的旧事件不会混入旧截止范围；记录时间是摄取时钟，不是事务提交时间，因此不宣称实现数据库历史时点快照。Loki、Prometheus 同样可能迟到写入；恢复时仍以已提交业务材料为准。

## 实际改动

- `observation_window.py` 只定义范围与时间约束，不包含 HTTP、SQL 或 Agent 调度。
- Loki、Prometheus 各增加显式窗口方法，复用白名单查询与解析逻辑。Loki 以传输端多一纳秒适配远端截止点语义，再同时严格校验流时间和事件时间；Prometheus 健康检查和区间查询使用同一截止点，窗口外样本直接失败。
- `change_records/window_models.py` 保存查询与响应契约，`window_api.py` 负责新只读端点；仓储增加可选 `recorded_until` 过滤，原查询行为保留。
- `change_validation.py` 提取共享记录校验；历史变更入口校验回显窗口、响应时间、记录身份、时间范围与截断，只转换历史事实，不调用当前运行清单。部署材料不会被标记为当前实例已经匹配。
- 新增 `GET /api/v1/changes/window`，参数为 `service_name`、`window_start`、`window_end`；空结果仍回显窗口，非法输入返回 422，端点只允许读取。该端点属于变更服务，未增加控制面调查启动选项。

## 测试方法与结果

验收日期：2026-09-07。遥测测试使用假 HTTP 响应和可推进时钟；仓储与 API 使用 Testcontainers PostgreSQL 17.6。没有调用真实模型、GitHub 或现场遥测服务，没有迁移现有业务数据库。

```powershell
uv run python -u -m pytest tests/unit/test_observation_window.py tests/unit/investigation/test_windowed_telemetry.py tests/unit/investigation/test_windowed_changes.py tests/api/test_change_window_api.py tests/api/test_change_record_api.py tests/integration/test_change_record_store.py tests/unit/investigation/test_loki.py tests/unit/investigation/test_prometheus.py tests/unit/investigation/test_changes.py tests/unit/investigation/test_github_changes.py tests/unit/investigation/test_live.py tests/unit/investigation/test_tools.py tests/unit/investigation/test_agent.py tests/unit/investigation/test_parallel_graph.py tests/unit/investigation/test_async_source.py -v --tb=short

uv run python -u -m pytest tests/unit/investigation/test_windowed_telemetry.py -k explicit_window_survives -v --tb=short

uv run python -u -m pytest tests/unit/test_observation_window.py tests/api/test_change_window_api.py -v --tb=short

uv run ruff check .
uv run ruff format --check .
uv run mypy
git diff --check
```

统一验收 **189 passed，耗时 27.09 秒**，其中 27 项使用真实 PostgreSQL。本片新增 47 个用例，覆盖查询窗口不漂移、来源响应时间越界、未来与非法参数在访问前拒绝、空结果、回显范围错误、迟到记录过滤及历史查询不读取当前运行清单。旧数据源、工具、图和变更 API/仓储回归通过。

补充调用进行中时钟推进断言后，日志、指标两个用例复验 **2 passed**；进一步明确先规范到 UTC 再检查整秒精度，避免特殊时区偏移绕过校验，时间契约与 API 复验 **21 passed**。这些为已有用例复验，不重复计入 189 项。Ruff 检查、格式检查和 mypy（204 个源文件）通过；本片 9 篇 Markdown 的结构与 229 个本地链接检查、`git diff --check` 均通过。

## 限制与下一步

- 新窗口方法已可独立调用，现有 Worker 继续走原相对时间入口；现场 v2 的任务时间窗和材料版本尚未装配。
- 历史查询保留服务白名单与结果上限；日志最多查询 100 条，不据此宣称保存了全部原始遥测。固定时间参数不能保证上游结果永不变化。
- 当前运行清单和 GitHub 组合来源暂时保留原 v1 路径。下一片设计并接入现场 v2 的运行级截止时间、来源范围、异步调用和快照恢复，同时明确这些来源的兼容与拒绝规则，不默默丢掉已配置能力。

## 面试表达

> 为了让多 Agent 的重试有一致调查范围，我给日志、指标和变更增加显式 UTC 窗口。模型失败时不能重新用“最近十五分钟”取证，否则调查依据会随时钟漂移。我区分了事件发生时间、记录时间和本次响应时间；历史发布查询不会拿当前运行实例证明过去部署状态。用假 HTTP 验证窗口不漂移和严格时间边界，再用真实 PostgreSQL 验证迟到入库、空结果及原接口兼容。固定参数本身不是不可变证据，稳定恢复由前一片的业务快照负责。
