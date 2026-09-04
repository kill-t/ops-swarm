# 63 - MVP 一键 Compose 交付与发布验收

## 目标

把控制面 API 和 Vue 生产页面纳入默认 Compose，使项目可以一条命令完整启动；执行跨模块发布回归和真实事故闭环，形成可重复的面试演示入口。

## 设计与边界

- `control-api` 使用现有 Python 运行镜像，只通过固定内部地址访问业务库、订单演练服务和 Prometheus。
- `web` 使用 Node 构建、非 root Nginx 运行的两阶段镜像，同源代理 `/api` 并关闭 SSE 缓冲。
- 两个新容器只绑定本机端口，使用只读根文件系统、非 root 用户、全部 capability 删除和零宿主挂载。
- 快速验收不调用模型；完整演示要求显式 `-AllowModelCall`，避免误产生费用。
- 演示只写本机业务审计数据，不访问生产环境，也不提供清理审计历史的破坏性命令。

## 实际改动

- Compose 新增 `control-api` 和 `web` 两个默认服务及健康检查。
- 新增固定 Node/Nginx 版本的前端生产镜像与 Nginx 同源代理配置。
- 新增 `scripts/verify-mvp.ps1`，验证三类故障并在 `finally` 中恢复安全状态。
- 新增 `scripts/demo-mvp-flow.ps1`，自动执行一次真实模型事故闭环并输出作战室地址。
- 统一修复 Ruff 格式检查发现的 6 处历史排版差异。
- 更新 README、架构入口和进度索引。

## 测试方法与结果

```powershell
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
uv run alembic check

cd web
npm test
npm run build
npm audit --audit-level=high
cd ..

docker compose config --quiet
docker compose up -d --build --wait
.\scripts\verify-mvp.ps1
.\scripts\demo-mvp-flow.ps1 -AllowModelCall
```

验收结果：

- Ruff 检查和格式检查通过，mypy 严格模式检查 `162` 个源码文件通过；
- 完整后端 `604 passed`，包含真实 PostgreSQL 17 集成测试；
- 完整前端 `21` 个测试文件、`84 passed`，类型检查和 Vite 生产构建通过；
- npm 依赖审计为 `0 vulnerabilities`；
- Alembic 未检测到 ORM 与迁移漂移；
- 默认 Compose 的迁移容器以 `0` 退出，其余 14 个服务运行，所有已声明健康检查的服务均为 healthy；
- Web 首页、`/follow-up` history fallback、直连控制面和 `/api` 同源代理均返回 `200`；
- 无模型脚本验证三种故障分别返回稳定错误码，结束后版本故障关闭、连接池恢复为 `50`、Redis 恢复可用；
- 控制面容器用户为 `10001:10001`，Web 容器用户为 `101`，两者根文件系统只读、capability 全部删除且无挂载。

真实闭环生成 `INC-000004`：Incident 状态依次经过 `open → investigating → awaiting_approval → mitigating → verifying → resolved → closed`，最终版本为 7。DeepSeek `deepseek-v4-flash` 单 Agent 一次尝试成功，2 次模型请求共 3114 tokens，耗时 6256 ms，估算费用 0.001877 美元；结论为 `release_500_regression`，Evidence Board 保存 3 条证据。确定性回滚成功，Prometheus 三次样本全部通过，随后生成不可变复盘报告和一条 open 改进行动项。

首次前端容器构建的 `npm ci` 因网络下载耗时约 5 分钟，构建随后成功；这是冷缓存构建成本，不是应用运行故障。

## 限制与下一步

- 当前仍是本地演练版，没有认证、RBAC、TLS 和生产部署配置。
- 发布验收已证明 MVP 主链路可运行，下一阶段不应继续横向增加功能；应整理简历描述、项目截图和 5～10 分钟面试演示讲稿。
- 若准备部署到公开环境，必须先增加身份认证、权限模型、密钥管理和真正的环境隔离，不能直接复用当前 Compose。

## 面试表达

> 我在发布验收阶段用完整回归和真实 Compose 运行发现交付缺口：API 和前端还依赖宿主机手工启动。于是我补齐非 root、只读文件系统的一键交付，并把验收拆成无模型快速冒烟和显式确认费用的真实闭环。最终 604 个后端测试、84 个前端测试全部通过，真实事故从告警、Agent 调查、人工审批、白名单执行、三次 Prometheus 验证一直走到关闭、报告和改进行动项，证明了状态持久化和安全边界在跨进程环境中真正成立。
