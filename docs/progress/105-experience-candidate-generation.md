# 105 - 经验候选契约、来源追踪与持久生成任务

## 目标

把已关闭事故的完整复盘转为可追溯、待人工审核的经验候选，先解决来源真实性、任务恢复与候选身份问题。开发基线是 main 的 `f2d77fb`；本片形成独立提交，不包含用户未跟踪的 `output/`。

## 设计与边界

设计先落在 [第 105 篇](../architecture/105-experience-candidate-generation.md)，并同步数据库、API 与第 64/65 篇架构入口。首版采用固定策略 `report-fact-draft-v1` 的确定性历史事实投影，不调用模型、归纳普遍规律或生成执行参数。候选保存为不可变 `pending_review` 版本，不能直接进入上下文，更不能继承历史审批。

只接受关闭事故且报告具有成功调查、主假设、批准计划、成功执行和通过验证的完整链。来源按报告的精确身份重建，核对报告及证据散列、引用闭包、审批计划内容散列、验证规则和样本；不能用当前 latest 或标题补造缺失材料。最多 20 条证据、100 个样本、128 KiB canonical JSON，超限拒绝而非裁掉反证。服务、本地环境、调查 frozen/live 与独立验证来源分别保留。

HTTP 只验证来源并提交持久任务。报告与策略组合唯一；重复请求返回原任务及首个请求者、授权记录。三个新接口始终要求可信 Bearer，工程师使用已有报告管理权限提交，兼容身份不能绕过。独立 Worker 采用 `FOR UPDATE SKIP LOCKED`、稳定任务身份、新尝试身份和最多三次尝试，来源错误终止，内部故障有界退避重试。候选和成功状态同事务保存；到期或已接管的旧尝试不能写回。

## 实际改动

- `experience/contracts.py`、`sources.py` 和 `projection.py` 分别负责有界契约与闭包、精确来源读取、历史事实投影；保留反证关系和全部验证样本引用，时间统一 UTC。
- `experience/store.py` 负责任务准入、竞争领取、租约校验、尝试结算与查询。查询和重复提交持有任务共享锁，避免 READ COMMITTED 下跨语句混读并发状态。
- `experience/api.py` 提供提交任务、读取任务和读取候选；请求不接受调用方指定身份、来源或策略，任务关联首个持久授权准入。
- `experience/worker.py` 和 `main.py` 提供独立常驻及 `--once` 命令；Compose 增加只连接业务数据库的 `experience-worker`。
- 迁移 `0062_experience_candidates` 增加三表、唯一/状态/时间/引用约束及不可变触发器。任务来源与请求、尝试身份和候选不得改写，终态被冻结；有事实时拒绝降级。既有迁移测试只更新当前 head 断言，历史目标不变。
- 单元夹具来自真实 PostgreSQL 上由假模型结果与固定健康探针完成的业务链导出；它是测试材料，不是现场事故证据。正常测试不写回夹具。

## 测试方法与结果

最终统一回归 **1,333 项通过**，用时 305.27 秒：单元 1,062 项、API 162 项、整组迁移 109 项。其中本片经验单元 27 项、经验 API/Worker 19 项、经验迁移 27 项；身份权限矩阵增加 5 项。数据库使用独立 Testcontainers PostgreSQL 17.6，不使用 SQLite。

Ruff、格式检查和 mypy 均通过；格式检查覆盖 598 个 Python 文件，mypy 检查 325 个源文件。Alembic 唯一 head 为 `0062_experience_candidates`，ORM 元数据一致性及空库完整升降级包含在迁移回归中。Compose 使用公开 `.env.example` 的配置检查通过；文档相对链接及 `git diff --check` 通过。

API/Worker 覆盖可信角色与兼容拒绝、缺失实体、来源不完整及漂移、严格请求体、并发幂等创建、竞争领取、过期接管、迟到结果、三次崩溃耗尽、失败退避、投影篡改、重复结算、独立进程和事务回滚。迁移覆盖来源缺失/null 拒绝、约束、唯一性、不可变事实、终态与降级保护。

```powershell
$migrationTests = @(Get-ChildItem -LiteralPath tests/integration -Filter '*migration*.py' | ForEach-Object { $_.FullName })
.venv/Scripts/python.exe -m pytest tests/unit tests/api @migrationTests -o addopts= -q --disable-warnings
.venv/Scripts/ruff.exe check src tests alembic
.venv/Scripts/ruff.exe format --check src tests alembic
.venv/Scripts/mypy.exe src
.venv/Scripts/python.exe -m alembic heads
docker compose --env-file .env.example config --quiet
git diff --check
```

独立审查发现任务查询跨语句读取时可能与 Worker 并发提交混读，现已修复为共享锁，并用门闩测试覆盖查询期间竞争领取和完成。额外故障注入让 PostgreSQL 拒绝候选 INSERT，验证候选与成功一起回滚、失败尝试保留且可重试；另以真实独立 Python 进程执行 `--once` 后查询持久结果。

本片不改图恢复策略，动态图文件中的变动仅为当前迁移 head 字符串；没有重新运行历史八项慢速动态图降级测试。没有调用外部模型、启动整套发布演示或升级用户 Compose 数据库。

## 限制与下一步

候选是原始历史投影，尚无人工确认、拒绝、启用、检索、停用和版本替代能力；也没有页面或模型上下文集成。第 106 步先交付可信工程师审核、确认与拒绝，审核绑定精确候选版本及散列，幂等和并发决定保留审计。后续再交付有效经验检索、停用与替代。

来源仍沿用现有复盘的主假设与单步骤处置契约，尚不消费 v3 开放假设或未来多步骤报告。

## 面试表达

> 我把经验生成与经验启用分开。先从不可变复盘按精确 ID 重建调查、审批、执行和验证来源，校验散列和证据闭包，再由独立持久任务生成待审历史投影。任务和尝试分离，租约阻止迟到结果，数据库保证候选和成功原子提交。重复请求和进程重启不会重复造候选；人工确认前，候选不能作为当前事故事实或执行授权。测试用真实 PostgreSQL 验证竞争、接管、事务失败和不可变事实，而不用假数据库代替这些语义。
