# 104 - 可信操作者身份与最小权限

## 目标与边界

为后续经验审核建立服务端可信主体，并在已有控制面命令上验证认证、权限和持久审计。首版仅服务隔离本地演练，使用环境配置的高熵 Bearer 凭据，不引入账号注册、密码登录、JWT、外部身份平台或经验生命周期。

## 状态与配置

`OPSSWARM_OPERATOR_AUTH_MODE` 支持 `legacy_local`（兼容默认）和 `bearer`。兼容模式仍接收人工自填 `actor_id`，不能作为可信身份；未来经验审核必须直接要求可信主体，不允许兼容绕过。`bearer` 模式缺少合法凭据配置时启动失败，不退回兼容模式。凭据仅从环境读取，配置包含凭据 ID、稳定主体 ID、角色、Secret 的 SHA-256 和带时区的过期时间。令牌格式为 `credential_id.secret`；Secret 至少 32 个 URL 安全字符，服务端按 ID 定位并恒时比较摘要。重复凭据 ID、重复摘要、同主体角色不一致、非法角色、无时区或不合法配置均拒绝。撤销和角色调整通过更新环境并重启控制面生效，已有请求不承诺中途撤回。

`OPSSWARM_OPERATOR_AUTH_CREDENTIALS` 为严格 JSON 数组，最多 32 项；每项字段为 `credential_id`、`subject_id`、单个 `role`、`secret_sha256`、`expires_at`。例如工程师、审批者使用不同主体和凭据，审批者不因身份可信而自动获得执行权限。可在本机终端用下面脚本生成一项配置和对应 Header；输出仅用于私下设置环境及调用 API，不能提交或放进普通日志。控制面只接收输出的配置数组，不接收原始 Secret。

```python
import hashlib, json, secrets
from datetime import UTC, datetime, timedelta

secret = secrets.token_urlsafe(32)
credential = {
    "credential_id": "engineer-key", "subject_id": "engineer-001", "role": "engineer",
    "secret_sha256": hashlib.sha256(secret.encode("ascii")).hexdigest(),
    "expires_at": (datetime.now(UTC) + timedelta(hours=12)).isoformat(),
}
print(json.dumps([credential]))
print("Authorization: Bearer engineer-key." + secret)
```

将数组设置为 `OPSSWARM_OPERATOR_AUTH_CREDENTIALS` 并设置模式为 `bearer`，重启控制面后，携带 Header 请求 `/api/v1/operators/me`。Compose 将同名变量仅注入 `control-api`，不授予业务 Agent 或 MCP 服务操作者凭据。

## 权限与接口

认证模式覆盖控制面所有 `/api/` 路由，包括预算、上下文、任务与 SSE 查询；健康检查和接口说明可公开。GET 使用只读权限，写入口使用显式操作名到权限的映射，未知写入口默认拒绝。角色为 `viewer`、`engineer`、`approver`、`executor`、`alert_source`：工程师可调查、生成计划、提交审批、管理报告并验证；审批者只能读和决定审批；执行者只能读、执行及验证；告警源只能摄取告警。权限范围为整个本地控制面实例，不声称支持租户或逐服务授权。

人工命令的 `actor_id` 改为可省略的兼容字段。认证模式从主体注入身份；自填不一致返回 403；兼容模式省略身份返回 422。认证、权限、身份冲突检查均先于业务仓储操作和幂等返回。`GET /api/v1/operators/me` 始终要求可信认证，返回主体、凭据 ID、角色、权限和过期时间，不返回凭据摘要。认证失败统一 401 并携带 Bearer challenge，越权 403，响应沿用错误契约。

## 持久化与失败路径

`operator_authorizations` 保存写命令的已授予准入事实：UUID、策略 `operator-auth-v1`、主体 ID、凭据 ID、角色及权限快照、操作名、HTTP 方法、路由模板、资源参数、原始请求体 SHA-256 和 UTC 授权时间。不保存令牌、凭据摘要或正文。准入记录先独立提交，再调用业务服务；它仅证明授权通过，不表示业务成功，409/404/422 也可能有准入记录。`X-Authorization-ID` 返回该记录的身份；业务实体和事件继续使用原操作者字段。读请求和未授权请求不生成准入记录。审计持久化失败则阻止命令。

认证判断基于匹配到的业务路由模板，不使用包含部署前缀的 URL，避免 `root_path` 或子应用挂载导致绕过。首版保护控制面请求准入，历史审批仍遵循原业务规则，不能把一份已获准执行的历史计划解释成具有可信审批来源。需要更强的审批来源约束时，应另行保存与业务决定原子绑定的认证来源并版本化迁移，不用准入记录倒推审批成功。独立故障演练服务的管理接口也不属于这套控制面认证范围。

迁移 `0061_trusted_operator_identity` 新增审计表，不改写历史操作者。审计记录不可通过应用 API 修改或删除，存在记录时拒绝降级，防止丢失授权证据。测试清理使用隔离数据库。认证模式通过命令行/API 验收；现有页面与原生 EventSource 尚未适配 Bearer Header，此模式不是前端完整认证交付。

## 验收标准

- 纯单元覆盖配置、过期、令牌格式、摘要、角色矩阵和默认拒绝。
- 真实 PostgreSQL/API 覆盖匿名、非法及过期令牌、越权和冒名阻断、主体注入、授权快照、重复与并发审批、乐观锁及迁移降级保护。
- 兼容模式保持既有 API 回归；静态检查、类型检查和 Git 差异检查通过。
- 经验候选、审核接口、图内人工恢复和前端登录不在本片内。

## 通用材料读取的兼容边界

`legacy_local` 只保留旧调查材料的原公开读取行为。人工重评 `human_review`、候选评估 `evaluate_candidate` 及既有回流阶段的模型输入、上下文清单和交互结果始终要求可信读取身份，与专属接口一致；不能通过通用调用入口绕过。Bearer 模式继续统一认证全部业务读取。
