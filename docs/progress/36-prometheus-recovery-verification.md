# 36 - Prometheus 指标与恢复验证

日期：2026-08-30

## 目标

让故障实验室拥有可在 Docker Desktop 中看到的真实 Prometheus，并让持久恢复验证 Worker 通过固定查询读取真实采集指标，而不是直接读取订单服务状态接口。

## 设计与边界

- Prometheus 是遥测存储，不替代 PostgreSQL 业务事实。
- `/metrics` 抓取时会执行真实 Redis `PING`，避免依赖状态长期陈旧。
- 三种恢复信号分别绑定代码内固定指标查询，客户端和 LLM 不能提供 PromQL。
- 查询结果必须满足单时间序列、标签匹配、数值范围和 10 秒新鲜度。
- 新验证运行使用 `prometheus_*_v1` 审计名称，历史 `local_*` 名称继续保留。
- 调查 Agent 本切片仍使用冻结证据，不顺带改变评测基线。

## 实际改动

1. 增加 prometheus-client，订单服务暴露三个恢复 Gauge 和一个低基数请求 Counter。
2. 新增 Prometheus 3.13 LTS Compose 服务、2 秒抓取配置、一天保留期和持久命名卷。
3. 新增受限 Prometheus 地址配置和固定查询恢复验证适配器。
4. 控制面默认验证服务与常驻验证 Worker 切换到 Prometheus 适配器。
5. 新增 `0018_prometheus_verification`，准确记录新验证器数据源并兼容历史审计。
6. 补充指标、固定查询、异常响应、旧样本、标签、范围和 URL 白名单测试。
7. 更新 README、系统架构、技术栈、数据库与既有恢复验证文档。

## 测试方法与结果

- 全量 363 项测试通过，包含新增指标、固定查询、真实 PostgreSQL、迁移和既有 Agent、审批、执行、验证回归。
- Ruff 规则检查通过，269 个文件格式正确。
- mypy 检查通过，115 个源码文件没有类型错误。
- Alembic 数据库与 ORM 元数据一致，没有遗漏迁移。
- `docker compose config --quiet` 通过。
- Docker Desktop Linux Engine 最初处于停止状态；使用 `docker desktop start` 启动后继续验收。
- `prom/prometheus:v3.13.2` 镜像存在且容器健康，`up{job="order-service"}=1`。
- Redis 正常时不可用指标为 `0`；代理断开后 Redis 与订单 5xx 指标变为 `1`；代理恢复后 Redis 指标重新为 `0`。
- 正式 `PrometheusVerificationAdapter` 对真实本机 Prometheus 查询得到 `redis_unavailable_ratio=0.0`，响应通过新鲜度与标签校验。
- `control-migrate` 成功升级到 `0018_prometheus_verification` 并以 0 退出。
- 验收结束后 Redis 代理保持安全默认 `available=true`。
- `git diff --check` 与最终 Compose 配置检查通过。

## 限制与下一步

- 当前是瞬时 0/1 Gauge，不是生产流量窗口统计。
- 只抓取订单服务，没有 Redis Exporter、控制面与 Worker 指标。
- 调查 Agent 尚未读取真实 Prometheus。
- 下一步实现受限的 Prometheus 调查数据源，保持冻结数据集用于公平评测。

## 面试表达

我没有把 Prometheus 接入理解成“能执行任意 PromQL”。验证步骤必须先匹配动作和目标白名单，再映射为固定查询；查询结果必须唯一、标签正确、数值有限且足够新。即使 Prometheus 返回 HTTP 200，旧样本或多序列也会被拒绝。最终恢复结论仍由持久调度和 PostgreSQL 事务决定。
