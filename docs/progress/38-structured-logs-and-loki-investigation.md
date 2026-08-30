# 38 - 结构化日志与 Loki 只读调查数据源

## 目标

让三类故障产生可重复、有限字段的 JSON 日志，并让 Agent 的 `query_service_logs` 工具通过项目内 Loki 返回规范化日志 Evidence。本切片不调用模型、不需要 API Key，也不切换持久调查 Worker。

## 设计与边界

- 订单服务只记录固定路由模板，不记录实际订单号或任意异常消息。
- 使用项目专用 JSONL 命名卷和 Alloy 文件采集，不挂载 Docker Socket。
- 模型只传 `order-service` 与 1～60 分钟窗口，不能提交 LogQL、URL、标签或数量上限。
- 单次最多读取 100 条、每行最多 2 KiB，三种故障最多聚合成三条 Evidence。
- Loki 流标签严格限制为 `service` 和 `source`，日志级别、故障类型等字段留在 JSON 中。
- 真实日志与真实指标目前分别冒烟；变更源完成前不与冻结数据混用。

## 实际改动

- 引入 `structlog`，新增故障实验室 JSON 事件记录器及 FastAPI lifespan 资源关闭。
- 三种订单失败分支分别输出版本、连接池和 Redis 所需的有限诊断字段。
- Compose 新增固定版本 Loki、Alloy 和三个项目命名卷，日志输入卷对 Alloy 只读。
- 新增 Loki 单机 TSDB、24 小时保留及严格标签配置。
- 新增 `LokiOnlyInvestigationDataSource`、环境配置和无模型 `loki_smoke` 命令。
- 增加固定 LogQL、白名单地址、窗口、数量、行大小、标签、JSON 联合类型与双时间戳校验。
- 新增结构化日志、Loki 适配器和真实 LangChain 日志工具的单元测试。

## 测试方法与结果

定向测试：

```powershell
uv run pytest tests/unit/fault_lab/test_structured_logging.py tests/unit/investigation/test_loki.py tests/api/test_fault_lab_api.py -q
```

结果：全部通过；覆盖三类精确日志契约、敏感订单号排除、路径白名单、固定 LogQL、URL 与服务白名单、窗口和返回上限、标签漂移、坏日志、时间错配、重复日志、HTTP 失败及空结果。

真实 Compose 冒烟：

```powershell
docker compose up -d --build --wait
uv run python -m opsswarm.investigation.loki_smoke --lookback-minutes 15
```

结果：Loki、Alloy、订单服务及原有基础设施均运行；版本 500、连接池超时和 Redis 不可用各产生一次 HTTP 500，工具从 Loki 返回三条 `loki-fault-lab` Evidence。原始查询确认流标签只有 `service="order-service"` 与 `source="fault-lab"`。测试结束后版本故障为关闭、连接池容量为 `50`、Redis 为可用。

全量验收：

```powershell
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
```

结果：Ruff 检查通过，281 个文件格式检查通过，mypy 检查 120 个源码文件通过，409 个测试全部通过。

## 限制与下一步

- 当前日志只覆盖 `order-service` 三个确定性失败分支，没有访问日志、trace 或多服务注册表。
- JSONL 源文件尚未轮转，Loki 保留策略不会自动缩小源文件，只适用于低流量本地演练。
- Loki 与 Alloy 是本地单实例，不具备生产鉴权、高可用和长期存储。
- Alloy 在当前 Docker Desktop 下未强制只读根文件系统，但固定非特权用户、移除 capabilities、禁止提权且没有 Docker Socket。
- 实时变更记录尚未接入，持久调查 Worker 继续使用冻结证据。
- 下一步实现确定性变更记录模拟器和受限变更查询，再组合三类真实现场数据。

## 面试表达

我把三种故障日志建成精确 JSON 契约，通过只读文件卷和 Alloy 送入 Loki，没有暴露 Docker Socket。Agent 不能自由写 LogQL，只能给白名单服务和有限时间窗；适配器对 URL、标签、数量、事件字段和双时间戳做失败关闭校验，并把最多 100 条原始日志压缩为最多三条可引用 Evidence。真实遥测与冻结评测隔离，避免现场数据污染基准分数。
