# 本地多 Agent 发布运行手册

使用固定脚本模型复现工程链时，优先看[本地演示手册](local-demo.md)，其配置可从零生成。本页用于自配身份与模型的标准发布入口；两个入口共用本机端口，不能同时启动。

在仓库根目录执行，使用支持 `!reset` 和 `!override` 的 Docker Compose v2。创建忽略的 `.env.local-release`，可复制 `.env.example` 后填写下列字段。

- 按[可信身份约定](architecture/104-trusted-operator-identity.md)生成工程师、审批者、执行者及告警来源四组独立凭据。将配置 JSON 数组写入 `OPSSWARM_OPERATOR_AUTH_CREDENTIALS`，在本机私下保管对应的原始令牌。可另配只读角色。启动时检查各角色存在未过期凭据。
- 设置 `LLM_API_KEY`、`LLM_BASE_URL`、`LLM_MODEL`、`LLM_BUDGET_PROFILE_ID`、`LLM_CONTEXT_WINDOW_TOKENS`、`LLM_INPUT_COST_PER_MILLION_USD`、`LLM_OUTPUT_COST_PER_MILLION_USD`。模型端点必须可从容器访问并支持工具调用。预检只校验配置，调查运行仍须使用与 Worker 相符的模型预算配置。

```powershell
.\scripts\local-release.ps1 -EnvFile .env.local-release
.\scripts\local-release.ps1 -EnvFile .env.local-release -Action Status
.\scripts\local-release.ps1 -EnvFile .env.local-release -Action Stop
```

启动等待数据库迁移和独立 checkpoint 初始化完成，再装配现场调查、人工续接、审批后执行、恢复采样及六个回流阶段。配置校验失败时查看 `release-prepare`，迁移失败时查看 `control-migrate`。镜像标签和命名卷属于独立项目 `opsswarm-local-release`。停止保留数据卷，再次执行启动命令可复用。控制 API 的发布头观测、执行 Worker 和恢复采样器的单次演练 HTTP 请求等待上限均为 6 秒，派发使用固定业务窗口校验。

默认启动会构建当前代码镜像；只有确认本地镜像已对应当前代码时才使用 `-SkipBuild`。`-ScriptedDemo` 为演示封装入口内部使用的项目和 Compose 覆盖开关，演示配置准备、预检和报告请统一通过 `local-demo.ps1` 执行。

浏览器打开 <http://127.0.0.1:35173>，控制 API 就绪探针位于 <http://127.0.0.1:38080/health>。演练订单和变更 API 分别绑定本机端口 38001 和 38002。数据库、缓存与遥测服务留在容器网络。

演练时先确认容器 UTC 时间与本机 UTC 接近，告警采用变更服务返回的实际发生时间。通过协作台以工程师提交人工补充、准备和重评，使用独立审批者与执行者完成后续操作。成功场景检查连续三个 HTTP 200 和事故收敛。未恢复场景检查连续三个 HTTP 500、一次再调查、验证后窗口的新证据、独立复查和持久人工接管。重放提交和重启后核对原操作、账本及事故槽位。详细场景边界见[本地发布范围](architecture/local-multi-agent-release-scope.md)。
