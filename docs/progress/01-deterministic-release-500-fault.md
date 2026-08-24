# 01 - 确定性版本 500 故障模拟

日期：2026-08-24

## 目标

交付 OpsSwarm 第一个可独立运行和测试的功能切片：一个默认健康、可通过受限管理接口显式启用或关闭 `release_500` 故障的 `order-service`。它为后续告警、Incident 和调查流程提供稳定故障输入。

## 设计与边界

- 故障行为完全由代码和布尔开关决定，不使用随机数，保证测试和演示可重复。
- 故障默认关闭；启用后所有合法订单查询确定性返回结构化 500，关闭后立即恢复。
- 故障状态只存在于单个演练进程内，重启后重置。这是刻意限制，因为它不是 OpsSwarm 的业务事实；后续场景编排器负责记录演练步骤。
- Uvicorn 固定单 Worker，避免不同进程各自保存一份开关状态。
- 管理接口只允许 localhost 或 Compose 演练网络访问。本阶段不实现生产鉴权，也不得部署到生产环境。
- 本切片不实现告警接入、Incident、PostgreSQL、LLM、指标或前端。

## 实际改动

- 创建 Python 3.12 与 uv 管理的最小 `src` 布局工程。
- 增加独立于 FastAPI 的线程安全 `FaultController`。
- 增加健康检查、订单查询、故障状态查询和故障更新接口。
- 请求模型拒绝未知控制字段，订单标识只接受有限字符集。
- 增加非 root、只读文件系统、移除 Linux capabilities 的容器配置。
- 容器构建通过 `uv sync --locked --no-dev --no-editable` 使用 `uv.lock`，避免重新解析运行依赖。
- 增加控制器单元测试和 API 行为测试。
- 根据 Starlette 当前官方建议使用 HTTPX2，避免 TestClient 的旧 HTTPX 弃用警告。
- 更新 README 的本地运行、Docker Compose 和测试说明。

## 测试方法与结果

实际执行：

```powershell
uv lock
uv sync
uv run ruff check .
uv run mypy
uv run pytest
docker compose config --quiet
docker compose up --build -d --wait
docker compose ps
docker compose down
git diff --check
```

结果如下：

- `uv lock` 成功解析 35 个包，`uv sync` 使用 Python 3.12.7 创建本地环境。
- Ruff 检查通过。
- mypy 严格模式检查 6 个源文件，无类型问题。
- pytest 共 8 项测试全部通过，无警告。
- `docker compose config --quiet` 通过。
- 镜像使用 `uv sync --locked --no-dev --no-editable` 构建成功，容器健康检查通过。
- 端到端验证依次覆盖健康检查、默认正常订单、启用故障后结构化 500、关闭故障后恢复，全部通过。
- 容器运行用户为 `10001:10001`、根文件系统只读、`CapDrop=["ALL"]`。
- 验证结束后已移除本项目容器和网络，并停止由本轮启动的 Docker Desktop。
- `git diff --check` 通过。

首次容器启动时 Docker Desktop 未运行；后台启动过程中 WSL 正处于状态切换，导致 Docker Engine 主动退出。只读检查确认 `wsl -l -v --all` 恢复正常后再次启动，完整容器验证通过。过程中没有修改 WSL 或 Docker 全局配置。

## 限制与下一步

当前开关是单进程内存状态，不支持多 Worker，也不保存历史；这是本地演练服务的明确边界。下一切片将实现纯领域层 Incident 状态机及其单元测试，再接 PostgreSQL 告警幂等与不可变时间线，避免一次引入过多基础设施。

## 面试表达

> 我先构建了一个确定性故障源，而不是直接接入 LLM。故障开关默认关闭，启用后订单接口稳定返回结构化 500，关闭后恢复，因此后续 Agent 评测面对的是同一个可重复输入。控制逻辑与 FastAPI 路由分离，并通过单元测试和 API 测试分别验证，这为后续 Incident 和调查流程提供了可靠基线。
