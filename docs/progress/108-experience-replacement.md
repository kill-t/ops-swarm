# 108 - 经验版本替代与不可变追溯

## 目标

让可信工程师用较新确认的经验替代旧参考，使新查询排除旧工件，同时保留原文、来源和当时人工决定。基线为 main 的 `973e21e`，本片不读取或提交用户未跟踪的 `output/`。

## 设计与边界

先落盘 [替代设计](../architecture/108-experience-replacement.md)。确认工件、当前有效状态和替代关系分别管理；新旧正文均保持 version=1 和历史 confirmed，关系绑定精确 entry ID、正文版本、内容及来源散列。来源投影 replaced/state_version=2，与原 disabled/2 互斥，不通过改写正文伪造连续版本号。

首次替代要求双方 active/1、同服务/环境/来源模式，目标确认时间严格更晚。每个来源最多一个后继、每个目标最多一个前驱，可形成 A→B→C；严格时间顺序阻断环，不递归遍历链，也不支持分支、合并或回退。后继后来退休不恢复旧来源，原关系和原命令成功重放仍保留。

命令绑定双侧精确正文与预期状态，完整幂等包含原因。可信身份、权限与过期检查先于业务和重放；系统注入主体和准入。事实与成功业务结果原子提交，业务校验拒绝用保存点移除半成品后保存 404/409，数据库故障整体回滚；授权准入仍不能视为业务成功。

独立审查发现，单独让替代按 UUID 排序锁两行还不够：原检索按排名持多行共享锁，可能与替代构成循环等待。因此检索先有界选出排名前三，再统一按 UUID 升序逐行加共享锁，最后按原排名返回；替代、数据库插入守卫也使用同一顺序。拿到锁后以新语句复核停用和替代，避免锁等待前的旧子表快照。

## 实际改动

- `replacement_contracts.py`、`replacement_policy.py` 分离严格命令、不可变关系及纯版本/范围/时间/终态规则；`replacement_validation.py` 校验双侧工件、完整命令、首次可信授权和稳定关系身份。
- `replacement_store.py` 负责有序双行锁、首次资格、原成功重放、保存点及业务结果；`replacement_api.py` 增加一个可信工程师 POST，不接受调用方操作者或正文。
- `state_reader.py` 从停用仓储提取当前有效性读取职责，复核互斥终态和直接后继；不因后继当前状态变化而否定历史事实，也不递归读取后继状态。状态 API 新增 replaced 和可选 replacement，历史详情保持原样。
- `search_store.py` 采用独立 `experience-search-v2`，排除替代来源并分离排名与锁顺序；系统范围、最多三条、反证提示和原版本引用保留。不自动跟随后继、补齐查询或退回旧版本。
- 迁移 `0065_experience_replacement` 新增关系及业务结果两张不可变表，双侧复合外键和唯一约束保护精确引用及单一前后继；双向 BEFORE INSERT 守卫锁父 entry 后复核停用/替代互斥，原生写入也不能留下双终态。空新表可降级恢复旧停用语义，有新事实则拒绝。
- 25 个历史集成测试文件仅更新当前迁移 head 字符串，旧降级目标及图恢复语义保持原样；不改旧迁移、模型策略、图或 checkpoint。

## 测试方法与结果

定向替代 API 与迁移 **67 项通过**，用时 107.81 秒；全部单元 **1,180 项通过**，用时 70.57 秒。最终统一回归 **1,636 项全部通过**，用时 429.55 秒：单元 1,180 项、API 240 项、整组迁移 216 项。

本片新增纯规则 45 项、API 27 项、迁移 40 项，权限矩阵增加 5 项。单元覆盖双侧精确版本/hash、完整命令幂等、范围、时间顺序、前驱唯一、输入上限、已替代不能停用及双终态失败关闭。API 通过完整业务链创建确认历史，覆盖 A→B→C、后继停用后的原命令重放、原正文不变、退休后排除、权限/过期/伪造、两侧未知目标、相同及竞争请求、停用竞争、写后拒绝与 SQL 故障回滚。

真实 PostgreSQL 并发验证包括：

- 替代先锁定，检索等待后重新复核并排除来源；检索先持共享锁，替代等待查询快照结束。
- 用两种故障标签强制排名与 UUID 顺序相反，让替代暂停在第一把排他锁后，确认检索已等待再放开第二把锁，验证不会形成“排名锁/UUID 锁”死锁。
- 四项原生双会话 INSERT 竞争使用 pg_stat_activity 确认 Lock 等待，验证 VOLATILE 守卫等待后能看到已提交停用/替代；目标后停用允许，其他互斥组合拒绝。

Compose 公开环境配置、文档相对链接、历史测试差异及 Git 差异检查通过；独立最终复核未发现阻塞问题。Ruff/格式和 mypy 已通过，覆盖 632 个 Python 文件和 347 个源文件；迁移唯一 head 为 `0065_experience_replacement`。数据库测试均使用 Testcontainers PostgreSQL 17.6，不使用 SQLite 或外部模型。首次数据库启动因 Docker 引擎未运行而失败，经 `docker desktop start` 启动后按原命令完成定向验证，未改测试夹具绕过数据库语义。

```powershell
.venv/Scripts/python.exe -m pytest tests/api/test_experience_replacement_api.py tests/integration/test_experience_replacement_migration.py -o addopts= -q --disable-warnings -x
$migrationTests = @(Get-ChildItem -LiteralPath tests/integration -Filter '*migration*.py' | ForEach-Object { $_.FullName })
.venv/Scripts/python.exe -m pytest tests/unit tests/api @migrationTests -o addopts= -q --disable-warnings
.venv/Scripts/ruff.exe check src tests alembic
.venv/Scripts/ruff.exe format --check src tests alembic
.venv/Scripts/mypy.exe src
.venv/Scripts/python.exe -m alembic heads
docker compose --env-file .env.example config --quiet
git diff --check
```

一个子智能体承担持久边界和迁移测试并只读审查主实现，另一个实现纯规则测试；主 Agent 核实关键结论、实现应用/API、统一运行验收并提交。八项慢速历史动态图降级未重跑，相关文件只更新 head；本片未升级用户 Compose 数据库或重新执行完整发布演示。原生约束最小行与完整业务链夹具分别验证数据库和应用边界，均不称作现场证据。

## 限制与下一步

版本替代是人工选择两个已确认工件的直接关系，不提供正文编辑或自动推导通用规律，不返回递归链的“最新可用经验”。即时查询仍可能在后续调用发生变化，没有持久化模型实际使用的经验清单，也没有经验工具或模型消费。

第 109 步让经验进入独立版本的模型上下文并保存实际引用：选择明确角色，绑定实际使用的 entry ID、版本、散列与上下文策略，保护预算和恢复，旧运行不静默换输入。停用/替代只影响新的选择，不抹去历史调用材料；经验仍不是当前事故事实或执行授权。

## 面试表达

> 我把经验替代保存为可信人工确认的不可变后继关系，两个正文、来源和原审核都保留。首次替代绑定双侧精确版本并校验有效状态、适用范围和严格确认时间；行锁和数据库双向守卫使停用与替代互斥。还处理了多行锁顺序：排名用于选结果，UUID 用于统一加锁，避免检索与替代互相等待。后继后来停用不影响原命令重放，也不恢复旧经验；检索只返回本次有效集合，历史引用仍可解释当时看到的版本。
