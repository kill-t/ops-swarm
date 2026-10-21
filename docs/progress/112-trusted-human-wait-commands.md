# 112 - 可信人工补充与调查取消

## 目标

第 111 步已把当前冻结 v3 的终端路由交接为持久等待，但工程师无法补充或取消。本片实现 await_human 的可信命令、状态和业务结果查询，让人工介入先形成一个可运行、可审计的闭环。

基线为 main `55d70dc`。开始时只有未跟踪用户目录 `output/`，没有读取或提交该目录。主 Agent 负责契约、服务、API、整合和验收；一个子 Agent 先只读核查，再独占实现迁移及其测试、纯规则单元测试，最后独立复核。子 Agent 未单独提交。

## 设计与边界

架构来源见 [可信人工等待命令](../architecture/112-trusted-human-wait-commands.md)，同步第 4/5/64/65 篇。等待初始版本 1，成功补充或取消各生成一条不可变事件，版本连续递增。最多 15 次补充，每次 1～5 项、每项最多 1000 字，预留一次取消容量；材料区分 statement、constraint 和 question，不能冒充工具证据或自动进入模型。

命令绑定精确交接 ID/来源散列、等待版本、幂等键及原因，主体来自服务端可信 Bearer。按运行、主体和键识别完整规范命令重放；旧成功命令在后续补充或取消后仍返回原事件。原始 UTF-8 正文最多 64 KiB，与授权正文散列及规范命令绑定；授权准入和 applied/replayed/error 业务结果分开保存，合法业务 404/409 先提交审计再返回。

| 操作 | 父运行 | 父工作项/尝试 | Incident |
| --- | --- | --- | --- |
| 补充 | await_human | 保持 completed/handed_off 与原交接时间 | investigating |
| 取消 | cancelled，记录完成时间，不生成 result/metrics | 保持 completed/handed_off，无新租约 | investigating |

同一父工作项锁串行化命令和查询；审计页、状态和历史交接重新核对连续事件、命令正文及实际授权来源。等锁后及最后 flush 后检查凭据期限，过期整体回滚。取消不表示事故恢复、关闭或新一轮调查，不回收原预算、用量或 deadline。

graph-v15 已到 END，不能把旧 resume 当作人工恢复。本片只交付补充与取消；恢复的具体协议、输入消费、独立持久任务、图/阶段版本及可审计预算追加由下一片设计实现，不重新 start 或静默升级旧 checkpoint。

## 实际改动

- human_wait 契约、纯规则、授权正文核验、不可变事件/命令映射、来源重建和事务服务按职责分开。
- 新增状态及业务审计 GET、supplements/cancel POST 四个入口。即使 legacy_local 也要求可信 Bearer；写操作限有 manage_incident 权限的工程师，客户端主体/角色/授权字段拒绝。
- 新增 cancelled 运行状态及 run.human_input_saved/run.cancelled 持久任务事件；迟到 Worker 仍无有效租约，不能推进取消运行。
- 历史 terminal-handoff 查询和交接重放兼容合法取消；共享父工作项锁避免与取消混读。
- 迁移 0068 增加两张表、交接复合唯一约束、连续版本/授权/不可变守卫及事件与成功审计的延迟闭环；替换当前交接状态守卫，旧迁移不改。有业务事实时拒绝降级，空表可往返，metadata 与正式 schema 对齐。
- README 增加可信补充调用示例；前端仅补充 cancelled 类型，没有新页面。

## 测试方法与结果

使用仓库 .venv、正式迁移建立的真实 PostgreSQL 17.6 和独立 checkpoint 数据库；模型为真实 SDK 的假传输，未访问真实模型或生产环境。

```powershell
.venv/Scripts/python.exe -m pytest tests/unit tests/api --tb=short
$migrationTests = @(rg --files tests/integration -g '*migration*.py')
.venv/Scripts/python.exe -m pytest @migrationTests --tb=short
.venv/Scripts/python.exe -m pytest tests/integration/test_dynamic_worker_handoff.py tests/integration/test_investigation_store.py tests/integration/test_fixed_task_graph.py tests/integration/test_live_task_graph.py tests/integration/test_budgeted_graph.py --tb=short
.venv/Scripts/ruff.exe check src tests alembic
.venv/Scripts/ruff.exe format --check src tests alembic
.venv/Scripts/mypy.exe src
.venv/Scripts/python.exe -m alembic heads
npm --prefix web run typecheck
git diff --check
```

| 验收组 | 实际结果 |
| --- | --- |
| 全单元/API | 1,533 通过，700.62 秒，含本片纯规则 29 项和人工 API 11 项 |
| 全部 migration 文件 | 251 通过，275.00 秒，含本片 13 项；metadata、空库往返与历史版本保护通过，无循环外键警告 |
| 动态交接与旧 Worker/仓储/任务图五文件 | 55 通过，299.42 秒，含动态交接 9 项与旧路径 46 项 |
| 并发审查组复跑 | 2 通过，62.74 秒；历史交接断言加强为 PostgreSQL NOWAIT 确认实际锁竞争，不重复计数 |
| Ruff / 格式 / mypy | 通过；格式覆盖 668 个 Python 文件，类型检查覆盖 368 个源文件 |
| 前端 / 迁移 / Git | typecheck、唯一 head 0068_human_wait_commands、diff --check 通过 |

去重验收集为 **1,839 项 Python 测试**。初步单元 29 项、人工 API 9 项及迁移 13 项也通过，均已包含在最终验收组，不累加。新设计与进度文档的本地链接检查通过。未重新执行全部历史动态图慢速恢复场景，也未重新执行真实模型或 Compose 发布演示。

新增测试覆盖可信身份、正文冒名及大小、分类约束、原始 JSON 不同表示的同命令重放、同主体幂等与跨主体隔离、404/409 结果审计、分页、补充上限与保留取消容量、取消后的旧命令/交接重放、预算与父尝试不变、迟到 Worker 拒绝、原生不可变/防伪/保守降级，以及并发等待版本竞争和认证到期回滚。

独立审查发现授权锁等待后的期限复核及旧交接读取/取消混读窗口，已修复并增加对应回归；最终只读复核未发现阻断问题。

## 限制与下一步

仅在 Testcontainers 数据库执行迁移，未升级用户 Compose 数据库。仅支持第 111 步当前冻结 v3 的 await_human，不处理 await_candidate、现场 v3 或历史图。人工材料尚未进入上下文；没有恢复命令、预算追加、LangGraph interrupt/resume、候选评估器或浏览器操作页面。取消后事故保留 investigating，由后续明确生命周期处理，不自动关闭或重开。

下一片 113：针对未取消等待，推进可信人工输入消费与显式恢复任务。先固定输入快照、系统约束、后续持久任务及尝试身份、预算续接/过期处理、图与阶段版本和失败路径，再交付可运行恢复；不得复用历史审批或清零额度。

## 面试表达

“我把人工输入做成有版本的业务事实，而不是向聊天记录追加一句话。命令绑定可信主体、精确交接和等待版本；事件、业务结果与取消状态原子提交，原始请求还能追溯到授权。并发由父任务锁和版本检查收敛，旧命令取消后仍可重放。人工陈述不等于工具证据，已结束的图也不等于暂停，所以后续恢复必须有自己的任务、输入和预算契约。”
