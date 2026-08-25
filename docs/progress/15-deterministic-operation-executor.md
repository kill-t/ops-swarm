# 15 - 确定性白名单执行器与幂等操作

## 目标

把已批准的高风险回滚计划转换为一次受控、可审计、可恢复的本地演练操作，并明确区分“动作执行成功”和“系统恢复验证通过”。

本切片不允许任意 Shell、SQL、URL 或客户端动作参数，不实现生产权限、长任务执行 Worker 或恢复验证。

## 设计与边界

- 当前唯一白名单动作是把 `order-service` 的 `release_500` 故障设置为关闭。
- 执行前重新校验当前计划、精确版本批准、计划内容哈希、Incident 状态与乐观锁版本。
- 全局唯一幂等键标识一次逻辑操作；同键重试返回原记录，不同键不能重复成功执行同一步骤。
- 首次执行先持久化 `running` 和 15 秒租约，外部动作结束后用新读取的当前时间与租约令牌提交终态。
- 进程崩溃后只有相同幂等键能在租约过期后接管；外部动作使用幂等的“设置为关闭”，允许安全物理重放。
- 成功、失败分别通过领域动作把 Incident 推进到 `verifying` 或返回 `investigating`，并与时间线事件原子提交。
- 数据库与 HTTP 不构成分布式事务，因此只承诺逻辑幂等和可恢复的至少一次执行，不宣称物理恰好一次。

## 实际改动

1. 新增 `opsswarm.execution` 模块，包含严格执行模型、稳定错误、受限配置、适配器协议和本地故障实验室实现。
2. 新增 `operation_executions` ORM 与 Alembic `0009_operation_executions`，保存授权绑定、幂等键、租约、尝试次数、有限结果和失败审计。
3. 使用复合外键把执行记录精确绑定审批请求、计划版本和计划步骤；使用部分唯一索引限制同一步骤的运行与成功记录。
4. 新增执行仓储的领取、租约接管、终态提交和查询事务，并复用计划风险与审批内容完整性校验。
5. 新增固定 HTTP 白名单适配器；地址仅允许本机或 Compose 服务名，请求路径与请求体由代码固定。
6. 控制面新增步骤执行和执行记录查询接口，区分首次、重复、运行中、冲突与已持久化执行失败。
7. 将 HTTPX2 调整为生产依赖，并更新锁文件。
8. 增加适配器、配置、结果边界、真实 PostgreSQL 幂等并发、租约接管、租约过期、失败回流和 API 全链路测试。

## 测试方法与结果

定向验收：

```powershell
uv run pytest tests/unit/execution tests/integration/test_operation_execution_store.py tests/api/test_control_plane_api.py tests/api/test_control_plane_openapi.py -q
```

真实 Docker 演练先启用故障并确认订单接口返回 `500`，随后由 `FaultLabOperationAdapter` 执行固定关闭动作，最终故障状态为 `false`、订单接口恢复 `200`。执行租约仓储的 6 个真实 PostgreSQL 用例通过，覆盖成功幂等、适配器失败、同键并发、租约接管、未批准拒绝和迟到完成拒绝。

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

结果：全量 171 个测试通过，其中收集到 55 个真实 PostgreSQL 集成测试；153 个受 Ruff 管理的文件格式检查通过，Ruff、76 个源码文件的 mypy 检查、Alembic 元数据一致性和 Docker Compose 配置检查通过。本地业务库为 `0009_operation_executions (head)`，项目专属 `control-db` 与 `order-service` 容器均保持健康。

## 限制与下一步

- 当前 API 同步承担最长 3 秒的本地操作，不能直接扩展为长时间生产变更。
- 当前只有 `local_fault_lab_release_500_v1`，没有 Docker Socket、Kubernetes 或云平台写权限。
- 审批操作者仍是本地审计声明，不具备可信身份和角色授权。
- 执行失败后的新计划版本、重新审批和自动循环尚未实现。
- `succeeded` 只证明故障开关操作完成，不能证明业务恢复。

下一切片建立 `verification_runs` 与确定性恢复采样，消费计划中已冻结的验证规则，并把验证结论及样本持久化。

## 面试表达

我把执行做成独立于 Agent 的确定性安全边界。执行器只接受数据库中受校验的步骤，不接受命令、URL 或任意参数；执行前重新校验精确审批和计划哈希。PostgreSQL 记录全局幂等键、步骤唯一约束和短租约，进程崩溃后可由同键接管。数据库和 HTTP 之间无法做到原子提交，所以我没有宣称恰好一次，而是把外部动作设计为可安全重放的“设置目标状态”，并用有效租约阻止迟到实例提交结果。执行成功后仍必须进入独立恢复验证，避免把“操作完成”误当作“事故解决”。
