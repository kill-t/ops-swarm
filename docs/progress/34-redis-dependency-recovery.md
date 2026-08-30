# 34 - Redis 不可用确定性处置闭环

日期：2026-08-30

## 目标

让 `redis_unavailable` 从评测真值和根因语义推进为可运行、可审批、可恢复的本地确定性处置闭环。

## 设计与边界

- Redis 当前使用故障实验室中的布尔依赖状态，不冒充真实 Redis 集群。
- 新动作 `restore_dependency` 与配置回滚分离，目标固定为 `dependency + redis`。
- 计划只允许严格的 `expected_available=false → target_available=true`。
- 依赖恢复为高风险动作，必须经过精确计划版本的人工审批。
- 执行使用固定 HTTP 路径、CAS、幂等租约和有限审计结果。
- 验证只读固定状态，将可用性映射为 `redis_unavailable_ratio`。
- LLM 不参与风险、审批、执行或恢复判定。

## 实际改动

1. 扩展故障控制器、订单错误和 Redis 固定管理接口。
2. 新增依赖目标、严格参数、能力模板和 v3 风险策略。
3. 在审批仓储、执行仓储和适配器中同步开放独立 Redis 白名单。
4. 新增 Redis 比较并设置执行和安全重放结果。
5. 新增 Redis 不可用比例验证探针，并复用持久验证调度 Worker。
6. 新增 `0017_redis_dependency_recovery` 约束迁移及安全降级检查。
7. 补充领域、API、适配器、真实 PostgreSQL 和迁移测试。
8. 更新 README、系统架构、技术栈、数据库、API 和安全处置事实来源。

## 验收结果

- 全量 333 项测试通过，包含领域、API、真实 PostgreSQL、迁移和验证 Worker 回归。
- Ruff 规则检查通过，255 个文件格式正确。
- mypy 检查通过，111 个源码文件没有类型错误。
- Alembic 数据库与 ORM 元数据一致，本地业务库位于 `0017_redis_dependency_recovery`。
- `docker compose config --quiet` 与 `git diff --check` 通过。
- PostgreSQL 与订单演练服务健康，迁移容器以 0 退出，验证 Worker 持续运行。
- 真实 Compose 演练确认：Redis 不可用时订单返回结构化 500；CAS 恢复后不可用比例为 0.0，订单返回 200。
- 演练结束后 Redis 状态已恢复为安全默认值 `available=true`。
- 本切片未调用外部模型，不消耗模型 token，也不依赖模型 API Key。

## 限制与下一步

- 尚未启动真实 Redis 容器或模拟网络分区。
- 调查证据仍来自冻结评测场景。
- 作战室仍不提供可信审批和执行入口。
- 下一步优先接入真实 Compose Redis 与固定故障代理，仍禁止执行器访问 Docker Socket。

## 面试表达

Redis 恢复不是模型生成一条“重启 Redis”文本，而是从受支持根因进入固定模板。计划冻结目标和状态，审批绑定内容哈希，执行器 CAS 防止覆盖漂移，持久执行支持幂等重放，最后由独立 60 秒观察窗口判定恢复。这样每个关键状态都可审计、可恢复，而且 LLM 永远没有任意基础设施权限。
