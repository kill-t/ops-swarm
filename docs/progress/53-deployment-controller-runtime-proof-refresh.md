# 53 - 独立部署控制器与动态证明刷新

## 目标

解决第 52 步静态 Ed25519 证明过期后必须重启订单服务的问题。本步建立真正独立的本地签发进程，订单服务从固定地址按需获取短时证明，同时保持 Agent 只读、验证方无私钥和失败关闭。

## 设计与边界

- `deployment-controller` 只提供健康检查和无参数签发端点，请求方不能控制签名内容。
- 控制器单独持有 Ed25519 私钥；订单服务只持有预置公钥环，不信任响应携带的任何密钥。
- 控制器每次创建新 `proof_id`，默认有效 120 秒，可配范围为 30～300 秒。
- 订单服务只允许 Compose 控制器或本机 `18003` 的精确 HTTP 地址，路径固定，禁止代理环境，超时上限 3 秒，响应上限 64 KiB。
- 每次运行清单查询都刷新；网络、状态码、大小、JSON、签名、时效或身份绑定失败均返回 `503`，不缓存或回退。
- 动态刷新、静态签名证明和未签名声明互斥；原有两种模式继续兼容。
- Compose 控制器只在显式 `signed-runtime` profile 中存在，不改变默认故障演练。本地环境私钥会出现在容器元数据中，生产不能照搬，应换成 KMS/HSM 或 Secret 挂载。

## 实际改动

- 新增控制器设置、FastAPI 应用和独立 Uvicorn 入口。
- 扩展 Ed25519 模块，增加原始私钥 Base64URL 导入导出和公钥环独立解析。
- 新增运行证明 HTTP 刷新端口，用流式读取实施响应大小上限。
- 扩展订单服务配置与运行清单端点，动态模式每次刷新、验签和绑定当前身份。
- 新增 `runtime_inventory_proof_refresh_failed` 稳定错误码，保留原有过期、未生效和无效证明精确错误。
- Compose 新增端口只绑定 `127.0.0.1:18003` 的 `deployment-controller`，并为订单服务增加刷新地址与超时配置。
- 更新 `.env.example`、README、静态签名证明文档和新的完整架构事实来源。

## 测试方法与结果

先执行新功能的定向回归：

```powershell
uv run pytest tests/api/test_deployment_controller_api.py tests/api/test_fault_lab_api.py tests/unit/fault_lab/test_runtime_refresh.py tests/unit/fault_lab/test_runtime_proof.py
```

结果为 `63 passed`。覆盖每请求新证明、签名验证、私钥不出现在响应和设置表示、非 `200`、非法 JSON、64 KiB 超限、未知签名方、任意 URL 拒绝、配置互斥、动态 API 刷新以及刷新失败无兜底。

执行完整后端质量门禁：

```powershell
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run pytest
```

结果：365 个文件格式检查通过，Ruff 无问题，mypy 检查 149 个源码文件无问题，完整后端 `581 passed`，其中包含真实 PostgreSQL/Testcontainers 集成测试。竞态修复和应用边界二次身份绑定加固后都重新运行完整测试，均为 `581 passed`。本步没有修改前端文件或 Evidence 响应契约，因此没有重复执行前端门禁。

执行配置和格式验收：

```powershell
$env:COMPOSE_DISABLE_ENV_FILE = "1"
docker compose --profile signed-runtime config --quiet
git diff --check
```

Compose profile 配置有效，Git 差异没有空白错误。配置检查显式禁用自动 `.env` 读取，不会打印用户密钥。

真实 Compose 验收重新构建 `ops-swarm-runtime:local`，只在当前 PowerShell 进程内生成一次性 Ed25519 私钥、公钥环和实例 UUID，然后启动 `signed-runtime` profile 的控制器和订单服务。首次验收暴露了真实网络时序竞态：验签时间在发请求前采集，会比控制器随后的签发时间早几毫秒，从而把合法证明误判为尚未生效。修复为“收完响应后再取验签时间”，并添加回归覆盖。

第二次真实验收通过：控制器和订单服务都为 `healthy`，运行清单来源为 `deployment-controller-ed25519`，密钥编号精确为临时编号，连续两次请求的证明 ID 不同，实例身份精确绑定。结束后清理所有临时进程环境，删除含一次性私钥元数据的控制器容器，并用用户原有 `.env`/默认配置重建订单服务。最终订单服务健康，临时控制器容器数量为 0。验收没有读取或输出 `.env`，没有调用模型 API。

## 限制与下一步

- 控制器仍从启动配置获取实例和发布窗口，不是真实 Kubernetes 服务发现。
- 本地控制器还没有自身的 Prometheus 指标、结构化签发日志、限流和高可用。
- Compose 环境变量私钥只用于本地演练，生产必须使用专用密钥管理与工作负载身份。
- 下一步先为签发和刷新增加低基数 Prometheus 指标、有界结构化日志与故障可观测测试，然后再决定是否接入 Kubernetes 只读适配器。

## 面试表达

> 我没有让订单服务自己持有签名私钥，而是拆出一个只能签固定清单的独立控制器。订单服务从白名单地址刷新，用预置公钥验签，并对超时、响应大小、证明时效和当前身份全部失败关闭。真实 Compose 验收还发现了一个毫秒级时序竞态，最终通过把验签时钟移到响应之后解决。这个设计体现了最小权限、可验证信任链和现实分布式时序问题的处理。
