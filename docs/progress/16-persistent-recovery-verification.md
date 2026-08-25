# 16 - 逐次恢复验证与持久样本

## 目标

让执行成功后的 `verifying` 阶段拥有明确运行、不可变样本、时间窗口和确定性终态，避免把“操作完成”直接当作“系统恢复”。

本切片不实现定时调度、Prometheus 区间查询、前端或自动重新调查。所有环境访问仍限制在本地 Docker Compose 故障实验室。

## 设计与边界

- 验证运行唯一绑定 `succeeded` 操作，并冻结步骤中的信号、比较符、阈值、窗口和样本数。
- API 不等待 60 秒；每次命令只触发一个固定只读订单探针并持久化一个样本。
- 客户端只能提供样本幂等键，不能提交 URL、HTTP 状态或观测值。
- 同键样本直接返回；只读探针在崩溃后允许安全重读，最终追加由运行行锁串行化。
- 任一 5xx 或探针错误立即失败；只有至少 3 个健康样本且首末覆盖 60 秒才通过。
- 终态样本、验证运行、Incident 状态和时间线在同一事务提交。
- 测试使用可注入服务器时钟推进窗口，不缩短正式业务规则。

## 实际改动

1. 新增 `opsswarm.verification`，包含运行与样本状态、纯规则判定、稳定错误、受限配置和订单 HTTP 探针。
2. 新增 `verification_runs` 与 `verification_samples` ORM，以及 Alembic `0010_verification_runs`。
3. 使用复合外键把验证运行精确绑定操作记录、Incident 和步骤，数据库约束保护规则、生命周期、阈值与样本形状。
4. 新增验证仓储，支持运行幂等创建、样本同键去重、连续序号、观察窗口判定和终态事务。
5. 新增验证应用服务，把探针失败转换为有限、可审计的 `error` 样本。
6. 控制面增加创建验证、查询验证和追加样本三个接口及稳定错误契约。
7. 增加健康/故障真实探针测试、时间窗口、样本数、失败回流、探针错误、幂等、终态拒绝、API 和迁移测试。
8. 更新项目入口、系统架构、技术栈、数据库、API 和执行器边界文档。

## 测试方法与结果

定向验收：

```powershell
uv run pytest tests/unit/verification tests/integration/test_verification_store.py tests/api/test_control_plane_api.py tests/api/test_control_plane_openapi.py -q
uv run pytest tests/integration/test_migrations.py -q
```

真实 Docker 探针在故障关闭时返回 `http_5xx_ratio=0.0` 和 HTTP `200`，故障开启时返回 `1.0` 和 HTTP `500`；演练结束后故障已关闭，订单接口恢复 `200`。时间窗口测试通过注入 UTC 时钟在 0、30、60 秒取得三个健康样本，验证进入 `passed` 且 Incident 进入 `resolved`。

完整验收：

```powershell
uv run pytest
uv run pytest --collect-only -q -m integration
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run alembic check
uv run alembic current
docker compose config --quiet
git diff --check
```

结果：全量 184 个测试通过，其中收集到 61 个真实 PostgreSQL 集成测试；170 个受 Ruff 管理的文件格式检查通过，Ruff、86 个源码文件的 mypy 检查、Alembic 元数据一致性和 Docker Compose 配置检查通过。本地业务库为 `0010_verification_runs (head)`，项目专属 `control-db` 与 `order-service` 容器均保持健康。

## 限制与下一步

- 当前需要调用方在观察时点显式触发样本，没有持久定时调度器。
- 当前样本来自固定订单端点，不代表真实生产流量窗口。
- 只支持 `http_5xx_ratio less_than`，没有 Prometheus 或多信号验证。
- 验证失败回到调查后，新调查、计划版本和重新审批尚未自动串联。
- Incident 的关闭/复发动作和作战室 UI 尚未接入。

下一切片实现支持 `Last-Event-ID` 的可重放 SSE，从 PostgreSQL 时间线补发事件并持续推送，不使用进程内消息作为事实来源。

## 面试表达

我把恢复验证设计成持久的逐次采样过程，而不是阻塞 60 秒的接口。规则来自已执行计划，客户端不能伪造数值；同键样本幂等，探针只读且可以安全重读。数据库锁只包围样本追加和终态判定，不跨越网络。只有成功样本数量和真实时间跨度同时满足才进入 resolved；任何 5xx 或探针错误都会留下样本并回到调查。这样系统区分了动作执行成功和业务恢复成功。
