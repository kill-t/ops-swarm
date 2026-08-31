# 40 - 统一现场数据源与调查 Worker 安全切换

## 目标

把 Loki 日志、Prometheus 指标和持久变更记录组合成完整现场数据源，让调查任务显式选择冻结基准或真实现场，并保证重试、LangGraph 恢复、评分和页面展示都不会混淆两种语义。

## 设计与边界

- `data_source_mode` 在任务创建时写入调查运行，不能只依赖 Worker 启动环境。
- 冻结 Worker 只领取 `frozen`，现场 Worker 只领取 `live`；处理前再次校验模式。
- 现场组合器严格委托三个既有只读适配器，任何来源失败都进入任务重试，不回退到冻结证据。
- 现场运行保存结论、证据、质疑和工具调用数，但不计算依赖固定答案的准确率指标。
- LLM 仍没有故障管理、Shell、SQL、Docker Socket 或白名单执行权限。
- 三类工具完成后只允许模型提交结构化结论，默认工具预算为 6 次，不用放宽递归上限换取偶然成功。

## 实际改动

- 新增 `InvestigationDataSourceMode`、`investigation_runs.data_source_mode`、Alembic `0020_investigation_data_mode`，以及约束指标范围的 `0021_investigation_metric_scope`。
- 开始调查请求、调查响应和作战室投影增加数据源模式；Vue 显示“冻结基准”或“真实现场”。
- `InvestigationStore.claim_next` 按 Worker 数据源模式过滤持久任务，重试继续使用原运行模式。
- 新增 `LiveInvestigationDataSource`，把三个协议端口分别路由到 Loki、Prometheus 和变更服务。
- 新增 Worker `.env` 配置读取和 `OPSSWARM_WORKER_DATA_SOURCE_MODE`，默认保持冻结模式。
- 单 Agent 与多 Agent 处理器支持可空评测场景；现场成功运行的 `metrics` 为 `null`。
- `EvidenceCollector` 记录已完成工具名；LangChain `wrap_model_call` 中间件在三类证据齐备后只保留结构化结论工具。
- 新增统一无模型冒烟命令，以及组合委托、失败传播、配置、路由、迁移、API、作战室和前端测试。

## 测试方法与结果

后端全量验收：

```powershell
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
```

结果：Ruff 通过，308 个文件格式检查通过，mypy 检查 133 个源码文件通过，452 个测试全部通过。

前端验收：

```powershell
cd web
npm run typecheck
npm test
npm run build
```

结果：类型检查通过，7 个测试文件中的 14 个测试通过，生产构建成功。首次全量测试有两个组件因 Windows/JSDOM 并行启动超过默认 5 秒而超时，原样重跑全部通过，因此没有修改测试超时掩盖偶发环境抖动。

真实 Compose 三数据源冒烟：

- Loki 聚合到 8 条 `release_500` 日志；
- Prometheus 观察到 5xx 比例从 `0.0` 升到 `1.0`，数据库和 Redis 信号健康；
- 变更服务返回与日志 `release_version` 一致的发布记录；
- 三个 LangChain 工具各调用一次，总调用数为 3；
- 冒烟后 `release_500` 恢复为关闭；最终迁移版本升级为 `0021_investigation_metric_scope`。

真实模型持久任务：

- 首两次尝试稳定触发 `GraphRecursionError`；受限诊断证明首轮已调用三个工具，后续仍重复查询；
- 增加收敛中间件后，同一持久任务在第 3 次尝试成功，证明失败重试没有丢任务；
- 模型为 `deepseek-v4-flash`，根因为 `release_500_regression`，建议为 `rollback_release`；
- 工具调用数为 3，Evidence Board 保存 6 条现场证据，根因质疑为 `supported`；
- `data_source_mode=live` 且 `metrics=null`，没有伪造冻结评测准确率。

## 限制与下一步

- 调查 Worker 还是单次命令，后续需要冻结与现场两个可优雅停机的常驻工作池。
- 调查运行只保留最后错误和总尝试次数，缺少逐尝试 Token、费用、耗时和错误历史。
- 新增第四类调查工具时必须显式升级证据齐备策略与提示版本。
- 现场调查只支持 `order-service` 和本地故障实验室，不能连接生产环境。
- 下一步优先接入 OpenTelemetry，把 API 投递、任务租约、模型调用、Agent 节点和工具请求串成 trace。

## 面试表达

我把数据源选择做成持久业务状态，而不是部署时临时替换依赖。API 创建运行时冻结 `frozen/live`，Worker 只领取匹配任务，所以重试和 checkpoint 恢复不会混用证据。现场三数据源失败关闭，准确率只对冻结真值计算。真实 DeepSeek 运行暴露了重复工具调用，我没有提高递归上限，而是在证据齐备后用 LangChain 中间件只保留结构化结论工具，把调用数收敛到 3，并通过原持久任务重试完成验证。
