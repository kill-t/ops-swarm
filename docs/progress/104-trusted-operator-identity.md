# 第 104 步：可信操作者身份与最小权限

## 目标

为后续经验审核建立可复用的可信主体，在已有控制面调查、报告、审批与执行命令上验证最小认证、权限与审计链路。上一片第 103 步基线为 `0f8f587`，工作区只有未跟踪的用户目录 `output/`，本片未读取、修改或提交该目录。

## 设计与边界

先更新 [身份设计](../architecture/104-trusted-operator-identity.md)、数据库/API 设计及第 64/65 篇架构来源。新增显式 `bearer` 模式：服务器只从环境加载主体、单一角色、凭据 ID、Secret 摘要和过期时间，不接受客户端声明身份或角色。随机 Secret 仅经 Header 发送，服务端固定格式和长度，恒时比较 SHA-256，过期、未知或错误凭据统一拒绝。

默认 `legacy_local` 保留原演练接口的自填身份语义，不提供可信认证；带认证 Header 的兼容请求也拒绝，防止调用方误以为凭据已被验证。`operators/me` 在兼容模式仍返回 401。开启认证但缺少配置时拒绝启动，不自动降级。未来经验审核必须要求可信主体，不能继承兼容绕过。

权限范围为整个本地控制面。工程师可以调查、计划、提交审批、报告管理和验证；审批者可以读取与作出审批；执行者可以读取、执行和验证；只读角色不能写；告警源只摄取告警。认证不替代原计划摘要、风险、状态、乐观锁、审批、幂等或白名单执行校验。

## 实际改动

- `identity/models.py` 提供确定性角色和权限；13 个写入口逐项映射，未知写入口默认拒绝。
- `settings.py`、`authentication.py` 负责严格配置与本地摘要认证，禁止重复凭据身份/摘要及同主体角色冲突，序列化不暴露凭据摘要。
- `identity/api.py` 在应用层统一保护四组 `/api/` 路由，覆盖任务、预算、完整模型输入、证据与 SSE。新增带严格响应契约的 `/api/v1/operators/me`，健康检查保持可用。
- 七类人工请求的 `actor_id` 变为兼容可选字段；8 个命令（批准、拒绝分别计数）从认证结果注入主体，自填不同主体返回 403，兼容模式未填仍返回 422。
- `operator_authorizations` 以独立事务保存写命令的已授权准入，包括主体、凭据 ID、角色/权限快照、操作、路由、资源参数、请求体散列和 UTC 时间；不保存令牌、凭据摘要或正文。准入先落库，失败阻断命令，响应携带 `X-Authorization-ID`。
- 迁移 `0061_trusted_operator_identity` 新增审计表；存在事实时阻止降级。隔离测试清理纳入新表，历史降级测试的当前 head 断言更新，原回退目标和业务事实不变。
- Compose 只给 `control-api` 增加两个认证环境变量，`.env.example` 保持无真实凭据；生成配置的方法见专项设计。

授权准入记录不代表业务成功，它在业务调用前独立提交；409/404/422 也可能存在准入记录。原业务实体、操作者字段和事件仍是结果事实来源。历史审批没有被追认为可信来源，独立故障演练服务管理接口不在此控制面认证范围。

## 验证方法与结果

数据库验证使用 Testcontainers PostgreSQL 17.6，未升级用户 Compose 业务数据库，也未执行真实模型或环境处置。

- `uv run pytest tests/api tests/unit -q`：第一轮 1171 项通过，包含 1030 项单元和当时 141 项 API；其中新增身份领域单元为 203 项。
- 最终 API 全量与 8 项动态图事实降级保护合并回归，共 151 项通过：最终 API 为 143 项（新增子路径部署回归 2 项），动态图降级保护为 8 项。覆盖匿名/错误/过期/重复 Header、全部已注册 API、13 项写权限、主体注入、冒名阻断、授权持久化、并发与重复审批、相反决定和版本冲突、审计故障。
- 整组迁移测试 82 项通过，含 ORM 元数据一致性、空库完整升级/回退、新审计表、历史事实不改写及存在授权时拒绝降级。
- `uv run ruff check src tests alembic`、`uv run ruff format --check src tests alembic`、`uv run mypy src` 通过；mypy 检查 315 个源文件。
- `uv run alembic heads` 唯一 head 为 `0061_trusted_operator_identity`。
- `docker compose --env-file .env.example config --quiet` 通过，未读取真实 `.env`；变更文档的仓库内链接与 `git diff --check` 通过。

迁移组可用以下 PowerShell 命令复现：

```powershell
$migrationTests = @(rg --files tests/integration | Where-Object { $_ -match 'test_.*migration.*\.py$' })
uv run pytest @migrationTests -q
```

最终 API 与动态图降级组的等价复现入口为：

```powershell
uv run pytest tests/api -q
$graphTests = @(
  'tests/integration/test_dynamic_second_round_routing_graph.py',
  'tests/integration/test_dynamic_third_round_graph.py',
  'tests/integration/test_dynamic_third_round_planning_graph.py',
  'tests/integration/test_dynamic_third_round_review_graph.py',
  'tests/integration/test_dynamic_third_round_routing_graph.py',
  'tests/integration/test_dynamic_third_round_synthesis_graph.py'
)
uv run pytest @graphTests -k downgrade -q
```

独立审查实际复现过部署前缀绕过：使用 `request.url.path` 判断时，`root_path` 或子应用挂载可能使 `/api/` 判断失效。已改为使用实际匹配的路由模板，并对两种路径验证匿名 401、越权 403 和业务不写入。最终独立复核未发现剩余阻塞。

## 限制与下一步

首版没有登录页面、账号数据库、在线撤销、逐服务/租户授权或外部身份平台；环境调整和凭据撤销需要重启。当前前端没有 Bearer Header，原生 EventSource 也未适配，认证模式使用 HTTP 客户端验收，不能宣称认证作战室完整可用。

下一片为经验候选契约、来源追踪与持久生成任务；经验审核与启用仍未开放，候选不会自动成为已确认经验。图内人工恢复、规划 Agent 与多步骤处置继续按路线推进。

## 面试表达

> 我先把可信身份放到经验审核之前。控制面通过环境配置的高熵令牌摘要验证操作者，角色和身份由服务端决定，模型与请求正文不能扩权。全部业务路由统一认证，写入口逐项授权，人工命令注入可信主体。授权记录在执行业务前持久化，失败就拒绝命令；它只证明权限检查通过，业务成功仍由原事务与事件决定。测试同时覆盖并发审批、冒名、审计故障和子路径部署绕过，保留本地演练与历史审批的真实边界。
