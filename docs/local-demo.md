# 本地工程链演示

该入口从零生成演示身份，使用独立 Compose 项目 `opsswarm-local-demo`、独立数据卷和仓库内脚本模型，不需要旧案例或缓存脚本。它用于检查取证、预算、人工续接、审批、执行与持久化的工程链，不衡量真实模型效果。设计边界见[复现设计](architecture/local-demo-reproduction.md)。

## 准备与启动

在仓库根目录执行，需要 Python 3.12、`uv`、Docker Desktop Linux 引擎，以及支持 `!reset` / `!override` 的 Docker Compose v2。首次启动会构建当前代码镜像。

```powershell
uv sync --locked
.\scripts\local-demo.ps1 -Action Prepare
.\scripts\local-demo.ps1 -Action Start
.\scripts\local-demo.ps1 -Action Check
```

`Start` 包含幂等的配置准备，可省略单独 `Prepare`。已经构建当前代码镜像时，可加 `-SkipBuild`；代码变化后重新构建。启动会运行迁移、checkpoint 初始化和配置检查，再等待服务就绪。模型只在容器网络提供固定输出，不访问外部模型。

演示与[标准发布入口](local-release.md)共用本机端口 35173、38080、38001、38002，不能同时运行；两者数据卷分别归属各自项目。浏览器协作台为 <http://127.0.0.1:35173>。脚本通过同一协作台代理访问控制 API。

生成文件保存在忽略目录 `.pytest_cache/local-demo`。`config.json` 私下保存四类角色的 `credential_id.secret` 令牌，`demo.env` 保存配置及秘密部分的摘要。脚本读取旧配置时会在内存中补齐凭据 ID。请私下保管这些文件。凭据有效期八小时。过期后先停止原演示栈，选择新子目录，并在后续每条命令使用相同参数。

```powershell
.\scripts\local-demo.ps1 -Action Stop
.\scripts\local-demo.ps1 -DemoDirectory .pytest_cache/local-demo/session-02 -Action Start
.\scripts\local-demo.ps1 -DemoDirectory .pytest_cache/local-demo/session-02 -Action Check
```

新身份不会更新旧案例的审批和执行依据；每次 `Run` 都创建新事故。命令不应并发执行，也不要同时手工修改演练故障。

## 运行场景

`Check` 在业务写入前观测 35 秒容器 UTC 连续性，再检查匿名拒绝、四类角色与依赖健康。发现 UTC 倒退或与单调时钟偏差超过 100ms 时，预检将阻断案例并生成报告。预检不通过时停止栈并保留报告即可。

```powershell
.\scripts\local-demo.ps1 -Action Run -Scenario handoff
.\scripts\local-demo.ps1 -Action Run -Scenario recovery
.\scripts\local-demo.ps1 -Action Run -Scenario reflow
```

| 场景 | 成功报告要求 |
| --- | --- |
| `handoff` | 冻结 v15 先调查日志与指标，独立审查追加变更补查，第二轮完成后持久等待人工。重启演示项目的控制 API 和冻结调查 Worker，核对原等待、轮次及账本，再完成可信补充、准备、重评与候选评估 |
| `recovery` | 从新事故完成调查、人工补充/准备/实际重评、候选和计划；拒绝工程师审批，由审批者通过两阶段审批、执行者提交；重复命令复用同一任务；唯一 CAS 回滚、同一尝试三个 HTTP 200、事故 resolved、槽位释放且运行 settled |
| `reflow` | 从新事故完成相同续接；回滚后注入连接池故障，获得三个 HTTP 500 和未恢复收敛；登记验证后新窗口变更，消费一次回流，保留原期限和旧调用账本，持久进入人工接管，第二次准入被拒绝 |

`reflow` 暂停的只是本演示项目的恢复采样 Worker，用于保证故障先于采样；退出该段时尝试恢复 Worker。它不伪造业务样本、不改数据库或审批，也不替换运行时实现。模型响应中的证据身份取自实际输入；固定 Token 用量和演示价格只用于工程预算校验。

## 报告与停止

每次 `Check` / `Run` 输出独立的 `runs/<case-id>/report.json`，包含阶段、对象身份及关键断言，失败时保留业务事实，不盲目重发结果未知的写请求。

| 状态 | 含义 |
| --- | --- |
| `passed` | 所选检查或场景的全部断言通过；`check` 成功只代表预检 |
| `blocked_environment` | 业务案例开始前，依赖、时钟或身份检查阻断 |
| `failed` | 案例开始后请求、Worker 或业务结果不满足要求，需要根据报告和持久记录定位 |

```powershell
.\scripts\local-demo.ps1 -Action Status
.\scripts\local-demo.ps1 -Action Stop
```

`Stop` 保留容器、配置、报告和数据卷，也可在凭据过期后使用。不删除历史卷，不关闭 Docker Desktop。异常退出时先看报告，再通过只读 API、协作台和限定项目的 Compose 日志核对；不要直接重发旧命令。失败可能保留故障，下一次新案例会检查并恢复本演示的基线。

当前实际验收结果见[本批记录](progress/local-demo-reproduction.md)。脚本存在、组件测试通过或服务健康均不能替代三个完整场景的整栈验收。
