# 33 - 持久恢复验证调度 Worker

日期：2026-08-30

## 目标

让 `running` 恢复验证自动按冻结窗口采样，并在 API 或 Worker 重启后从 PostgreSQL 继续，不再要求工程师手工触发三次样本。

## 设计与边界

- 新增验证专用一对一任务，不改造成多态调查任务表。
- 任务、运行创建保持单事务；调度进度与样本均持久化。
- 使用 `SKIP LOCKED`、唯一租约令牌和确定性样本幂等键。
- 调度自身异常只退避，不冒充系统验证失败。
- Worker 只访问固定本地探针，不接模型、Shell 或 Docker Socket。
- 调度状态可通过只读 API 观察，租约令牌永不公开。

## 实际改动

1. 新增 `VerificationWorkItemStatus`、有限错误与严格持久读取模型。
2. 新增 `verification_work_items` ORM 表和 `0016_verification_work_items` 迁移。
3. 验证运行创建时同事务投递首个立即到期任务。
4. 仓储实现并发领取、过期租约接管、样本结算、下一次到期计算和异常退避。
5. 调度样本键由任务 UUID 与序号确定性生成，支持崩溃重放。
6. 新增常驻 `VerificationWorker`、一次运行诊断模式和安全参数配置。
7. 新增只读调度任务 API，不返回 `lease_token`。
8. Compose 新增一次性 `control-migrate` 和常驻 `verification-worker` 容器。
9. 增加领取、崩溃恢复、自动 60 秒窗口、API 和迁移测试。
10. 更新 README、系统架构、数据库、API 和恢复验证事实来源。

## 已完成的定向验证

- 12 项 Worker 与探针单元测试通过。
- 34 项恢复验证、迁移与控制面真实 PostgreSQL 测试通过。
- 样本已落库但任务未结算时，新 Worker 接管后未重复调用探针。
- 可注入时钟在 0、30、60 秒自动形成三个样本并解决 Incident。
- 本地业务库位于 `0016_verification_work_items (head)`，`alembic check` 无漂移。
- Compose 中迁移容器退出码为 0，PostgreSQL 与演练服务健康，验证 Worker 常驻运行。
- 全量 309 项测试通过，包含单元、API、真实 PostgreSQL、迁移和 Worker 恢复测试。
- Ruff 检查与格式检查通过，252 个文件格式正确。
- mypy 检查通过，111 个源码文件没有类型错误。
- `docker compose config --quiet`、`git diff --check` 均通过，真实 `.env` 仍被 Git 忽略。
- 容器验收结果：PostgreSQL 与订单演练服务健康，迁移容器以 0 退出，验证 Worker 持续运行。
- 本切片未调用外部模型，不消耗模型 token，也不依赖模型 API Key。

## 限制与下一步

- 当前使用短轮询，没有 `LISTEN/NOTIFY`。
- 只保存最近一次 Worker 有限错误，不保存每次调度尝试历史。
- 真实 Prometheus 区间指标和多信号恢复规则尚未接入。
- 下一步优先补齐 Redis 不可用故障的计划、审批、执行和验证链路。

## 面试表达

恢复窗口不能依赖进程内 sleep。我把调度状态放入 PostgreSQL，用租约和 `SKIP LOCKED` 支持多 Worker，并让每个调度序号生成稳定样本幂等键。这样 Worker 在外部只读探针、样本提交或任务结算的任意边界崩溃，都能在不重复业务样本的前提下恢复。调度故障与系统恢复失败是两个不同状态，前者退避重试，后者才驱动 Incident 回到调查。
