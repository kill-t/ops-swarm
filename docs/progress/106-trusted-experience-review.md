# 106 - 可信工程师经验审核、确认与拒绝

## 目标

在第 105 步不可变候选之上，增加可追溯的可信人工确认边界。开发基线为 main 的 `e9dd378`，本片不读取或提交用户未跟踪的 `output/`。

## 设计与边界

设计先落在 [第 106 篇](../architecture/106-trusted-experience-review.md)。审核与候选工件分开：原候选及生成时待审措辞保持不可变，新的审核投影从版本 0 单向进入版本 1 的 confirmed/rejected。可信工程师绑定精确候选版本、内容散列和审核版本提交决定；处置审批者没有经验审核资格，legacy 身份不能放行。

确认时决定、独立经验版本和业务成功结果同事务保存；拒绝不产生经验。`confirmed-experience-v1` 外层明确当前人工确认事实，内层保存原候选快照；确认不改变历史假设强度、来源模式、反证或授权边界。暂不提供停用、替代、列表检索或上下文消费。

幂等键按候选和主体作用域绑定完整规范化命令。同主体同键同命令重放原结果，不覆盖首次审核人和授权；同键异参、错误版本、内容散列或终态反转拒绝。首次决定重新核对精确源记录；成功重放只校验冻结工件和原决定，不重新批准或改写后来变化的历史来源。

授权准入只代表权限检查。另存不可变业务结果，覆盖 applied、replayed 和带有限错误码的 409/404；错误在审计提交后才返回。结构无效请求不进入业务审核，未分类数据库故障整体回滚，不能把保留的准入说成已持久化失败结果。

## 实际改动

- `review_contracts.py`、`review_policy.py` 分离严格命令、确认内容及确定性版本/幂等/终态规则；`review_validation.py` 校验决定、授权、候选和经验的同源关系。
- `review_store.py` 组织候选行锁、来源重建、保存点、原子确认和业务结果审计；查询持共享锁，避免并发审核造成跨语句混读。
- `review_api.py` 提供提交审核、读取当前审核和按精确 ID 读取确认版本三个可信接口；身份由认证结果注入，不接受自填审核人或正文。
- 原生成仓储的纯工件校验提取为 `artifact_validation.py`，供生成与审核共用，保留第 105 步 API 和候选的原语义。
- 迁移 `0063_trusted_experience_review` 新增审核、确认版本和业务结果三表；复合外键绑定精确候选及 confirm 决定，唯一约束保护单候选单决定，触发器禁止改写/删除。空库可回退，存在任何新事实则拒绝降级；不回填旧候选内容。
- 新写入口复用当前仅工程师拥有的 `manage_report` 权限，三个新入口全部要求 Bearer。历史测试文件仅更新当前迁移 head，历史回退目标不变。

## 测试方法与结果

统一回归 **1,428 项通过**，用时 311.04 秒。随后补充写入后业务校验拒绝的保存点验收，并对最终审核 API 整组重跑，**29 项通过**，用时 51.29 秒；累计覆盖 **1,429 个不同用例**：单元 1,090 项、API 191 项、整组迁移 148 项。

本片新增审核纯规则 23 项、审核 API 29 项、审核迁移 39 项，权限矩阵增加 5 项。覆盖精确版本与散列、规范化完整命令幂等、主体隔离、并发确认/拒绝、过期身份、业务拒绝结果审计、首次来源漂移、历史成功重放、未知目标、事务故障与保存点回滚。数据库均为 Testcontainers PostgreSQL 17.6，不使用 SQLite。

Ruff、格式与 mypy 通过；格式检查覆盖 609 个 Python 文件，mypy 检查 332 个源文件。唯一迁移 head 为 `0063_trusted_experience_review`，包含 ORM 元数据一致性和完整空库升降级回归。Compose 公开环境配置、变更文档相对链接与差异检查通过。独立复核未发现剩余阻塞问题。

```powershell
$migrationTests = @(Get-ChildItem -LiteralPath tests/integration -Filter '*migration*.py' | ForEach-Object { $_.FullName })
.venv/Scripts/python.exe -m pytest tests/unit tests/api @migrationTests -o addopts= -q --disable-warnings
.venv/Scripts/python.exe -m pytest tests/api/test_experience_review_api.py -o addopts= -q --disable-warnings
.venv/Scripts/ruff.exe check src tests alembic
.venv/Scripts/ruff.exe format --check src tests alembic
.venv/Scripts/mypy.exe src
.venv/Scripts/python.exe -m alembic heads
docker compose --env-file .env.example config --quiet
git diff --check
```

使用固定调查结果和假健康探针完成真实 PostgreSQL 业务链，不调用外部模型，不把测试夹具写成现场证据。数据库故障注入分别阻止经验 INSERT 和业务结果 INSERT，检查审核、经验、结果一起回滚，撤销测试故障后原命令可重新成功。

独立子智能体负责三表 ORM、固定迁移与迁移测试，再只读审查主实现；主 Agent 核实模型和数据库边界、统一运行验收及提交。没有重新运行八项慢速历史动态图降级测试；这些文件仅改变当前 head 字符串，没有修改图实现或恢复语义。未升级用户 Compose 数据库或重跑完整发布演示。

## 限制与下一步

已有精确确认版本及可信决定，尚无有效状态治理、停用、替代、确定性检索或模型上下文引用；当前候选 GET 返回生成时工件，审核状态读取新 review 接口。第 107 步建立有效经验状态、停用与有界确定性检索，只有已确认、有效且范围匹配的版本能被检索；后续再补版本替代和上下文引用。

## 面试表达

> 我把候选工件、可信人工决定和确认版本分别保存，不通过改写原文伪造审核历史。审核命令绑定精确版本及散列，候选行锁和唯一约束使并发确认/拒绝只有一个胜者；同主体原命令可重放，但不能借幂等改意见。确认决定、经验和成功审计原子提交，409 冲突也有独立业务结果，授权准入不再被误解成业务成功。确认只是把一段有来源和适用范围的历史材料变成人工认可的参考，不等于当前根因或执行授权。
