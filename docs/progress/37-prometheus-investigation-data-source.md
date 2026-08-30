# 37 - Prometheus 只读调查数据源

## 目标

让 Agent 的 `query_service_metrics` 工具读取本地 Compose 中真实 Prometheus，并输出经过白名单、范围和响应校验的规范化指标 Evidence。整个切片不调用 LLM、不要求模型 API Key，也不改变冻结评测数据集。

## 设计与边界

- 模型只传 `order-service` 和 1～60 分钟回看窗口，不能提交 PromQL、URL 或指标名。
- 先执行固定 `up` 查询确认目标当前可抓取，再执行固定 `query_range` 读取三个故障 Gauge。
- 60 分钟窗口每条序列最多 121 个求值点，防止网络响应和模型上下文无界增长。
- 三个序列聚合为一条 Evidence，只保存首值、末值、最值、样本数和规则需要的派生属性。
- 实时日志与变更源尚未接入时返回空集合，不拿冻结数据冒充现场数据。
- 持久 Worker 和评测命令继续使用冻结场景，避免实时指标与固定答案或冻结日志混用。

## 实际改动

- 新增 `PrometheusOnlyInvestigationDataSource` 和 `PrometheusInvestigationSettings`。
- 新增固定抓取健康查询、固定指标查询和 `vector` / `matrix` 响应模型。
- 增加目标服务、URL、时间窗、序列数量、标签、样本数量、时间顺序和值域校验。
- 新增有限错误 `InvestigationDataSourceFailure`，网络和响应问题不会退化成“无异常”。
- 新增 `prometheus_smoke` 命令，通过 LangChain 实际使用的 `query_service_metrics` 工具执行无模型冒烟。
- 更新环境变量示例、README、单 Agent 基线与 Prometheus 架构文档。
- 新增 19 个单元测试，覆盖工具收集、固定查询、窗口上限、空端口、非法目标、非法窗口、目标失联、缺失指标、错误标签、非法值、HTTP 失败和不安全地址。

## 测试方法与结果

定向检查：

```powershell
uv run pytest tests/unit/investigation/test_prometheus.py -q
```

结果：19 个测试通过。

真实 Prometheus 冒烟：

```powershell
uv run python -m opsswarm.investigation.prometheus_smoke --lookback-minutes 1
```

结果：通过 Agent 指标工具返回一条 `prometheus-fault-lab` Evidence，包含三个指标的窗口摘要；当时 5xx 最新值为 `0`、连接池饱和度为 `0.2`、Redis 不可用比例为 `0`。

随后在 `try/finally` 中打开版本 500 故障、等待 Prometheus 抓取并查询，5xx 最新值和最大值均为 `1.0`；关闭故障并再次抓取后，最新值恢复为 `0.0`，窗口最大值仍为 `1.0`。最终 `GET /__admin/faults/release-500` 确认 `enabled=false`。

全量验收：

```powershell
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
```

结果：Ruff 检查通过，274 个文件格式检查通过，mypy 检查 117 个源码文件通过，382 个测试全部通过。

## 限制与下一步

- 指标是故障实验室快照 Gauge，不代表真实生产流量统计。
- 只支持 `order-service`，尚未由持久服务目录生成查询注册表。
- 真实日志、部署与配置变更尚未接入，因此持久 Worker 暂不切换到现场数据源。
- 下一步增加结构化 JSON 日志和 Loki，使用固定 LogQL 输出规范化日志 Evidence；之后再接变更记录，三类现场证据完整后切换运行时 Worker。

## 面试表达

我把 Prometheus 接到了 Agent 的指标工具，但没有开放自由 PromQL。Agent 只能选择白名单服务和有限时间窗；适配器先检查 `up=1`，再执行固定区间查询，并对序列数量、标签、时间和值域做失败关闭校验。最多 121 个原始点会被压缩成一条可审计 Evidence。实时数据与冻结评测彻底隔离，所以真实工具调用不会破坏单 Agent、多 Agent 对照的可重复性。
