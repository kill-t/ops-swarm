# 126 - 远端发布持久窗口与受限派发

## 目标

在main的068f2e5上落实第125步交接要求的远端截止与在途隔离。继续前核对AGENTS、状态/进度、123～125相关设计、Git与实际代码。初始只有用户output/目录未跟踪，未读取或提交该目录。

## 设计与边界

事实来源为[专项设计](../architecture/126-bounded-release-dispatch.md)。125回执能查询历史，但缺少远端约束时，控制面失租/取消后仍可能有迟到写入。本片将原126目标中的远端协议作为一个完整可运行切片：注册固定窗口、实际CAS、查询、取消与原生原子保护。控制面可信准入、任务及Worker在127接入，本片不是该完整业务链路的验收。

仅操作本地order-service的rc1→stable，原键为candidate-release-{规范operation UUID}，窗口最多30秒且不可续期。数据库clock_timestamp在锁后、命令插入和延迟提交约束检查；过期是prepared的投影，不伪造新的成功命令。注册不改变版本，只有窗口内原CAS应用成功才同时推进lab状态和远端操作。

## 实际改动

- 0081_lab_release_dispatch新增lab_release_dispatches：不可变原命令/摘要、固定窗口、创建时刻、prepared/applied/cancelled及成功命令ID/终止时刻。内容、窗口和终态不可改写，事实不可删除，有数据禁止降级。35个历史测试文件只更新当前head断言，已发布迁移未修改。
- 新增dispatch_contracts/tables/queries/store/api，分离结构/确定性约束、ORM、完整重建、事务与HTTP。POST /__admin/simulated-release/dispatches注册；GET/PUT/DELETE /{operation_id}查询、执行和取消。执行/取消只接受操作ID及空正文，不能临时改参数。
- 首次注册/执行201，精确重放200；同ID异命令/窗口409，不续期。取消prepared首次及重放200，应用后取消409，不能承诺撤销副作用。ready只表示窗口有效，不是业务授权或当前CAS证明。成功和业务错误响应均no-store，GET包含完整原操作及125回执。
- 应用锁序为头行→操作，查询用头共享锁。原生候选命令守卫对规范候选键要求精确同源prepared窗口；旧PUT/直接SQL不能用同一键绕过取消或截止。原命令触发器推进版本后原子绑定applied，禁止单独伪造应用；延迟约束校验截止及命令/操作xmin同事务，保存点兼容。头/操作由触发器改变后显式刷新ORM，再作完整重建。
- 业务审批、124 prepared/dispatch_eligible=false、原Incident/父运行/预算/活动槽位均保持。该管理入口不承担业务批准或身份委派，也不封禁其他显式旧演练管理键。未知结果只读原ID/键，不能自动重发或换键。

## 测试方法与结果

验收日期2026-10-26。采用JUnit均零失败、零错误、零跳过；按classname/name去重共2,157项相关测试通过。

| 范围 | 真实结果 |
| --- | --- |
| 全量单元 | 2,112项，44.13秒；包含新增窗口/键/终态25项 |
| 新远端协议 | 9项，27.53秒；元数据/空迁移往返、实际CAS/幂等/重启、取消和竞争、锁等待越期、长事务整体回滚、原生绕过/不可变/保存点/带数据降级、响应丢失只读核对及损坏拒绝 |
| 回归与迟到补验 | 37项，164.84秒；旧发布API10、回执API6、原生15、通用迁移3、第124步双来源2，加新锁等待用例1（重复）并补验伪造窗口内创建时刻的迟到SQL仍被数据库当前时钟拒绝 |
| 空正文/缓存头补验 | 1项，11.36秒，与新协议用例重复；确认未知操作404禁止缓存、执行不能注入临时命令 |
| 专项单元 | 25项，0.88秒，与全量单元重复 |
| 静态 | Ruff通过、855文件格式一致、Mypy497源文件通过；唯一head为0081_lab_release_dispatch；差异及731个修改文档本地链接检查通过 |

```powershell
.venv/Scripts/pytest.exe tests/unit -q
.venv/Scripts/pytest.exe tests/api/test_lab_release_dispatch_api.py -q
.venv/Scripts/pytest.exe tests/api/test_lab_release_api.py tests/api/test_lab_release_receipt_api.py tests/integration/test_lab_release_migration.py tests/integration/test_migrations.py tests/api/test_candidate_release_request_api.py::test_two_sources_bind_actual_lab_get_without_dispatch_or_parent_mutation tests/api/test_lab_release_dispatch_api.py::test_lock_wait_does_not_extend_deadline_and_expired_cannot_be_renewed -q
.venv/Scripts/pytest.exe tests/api/test_lab_release_dispatch_api.py::test_register_execute_replay_restart_and_read_only_receipt -q
.venv/Scripts/ruff.exe check src tests alembic
.venv/Scripts/ruff.exe format --check src tests alembic
.venv/Scripts/mypy.exe src
.venv/Scripts/alembic.exe heads
git diff --check
```

延迟提交测试先在事务中实际变更版本及派发为applied，再等到截止后提交，确认整个命令/状态/派发回滚。竞争测试只允许应用/取消单胜。模拟响应丢失前实际提交，随后只读原事实且无新副作用。首轮发现触发器更新后头行ORM缓存过期，修正显式刷新后重跑通过，早期失败不计入验收。

仅隔离Testcontainers PostgreSQL17.6、ASGI和固定/数据库时钟，无真实模型或生产调用，未升级用户Compose库。测试中的实际CAS是隔离演练数据库行为，不是真实镜像发布或恢复健康。主Agent完成设计/实现/关键核查/统一测试；审查子任务因模型容量未执行，未作为验收证据。报告位于忽略的.pytest_cache/slice126-*.xml，未提交。

## 限制与下一步

截止保护数据库应用决策及延迟约束时点，不承诺网络响应或物理提交在截止前到达。取消等待已获得头锁的执行事务，若先应用则只能返回冲突；本地取消不自动等于远端取消。未交付业务执行Worker、租约恢复、控制面unknown结果或父运行结算。

127接入精确124请求的可信执行准入、独立持久任务/尝试与Worker；远端窗口不得晚于准备/原运行/租约期限。写出前重新核验届时身份、审批、预算及CAS；历史授权不作永久委派。unknown只读查询原操作/回执并保存材料与决定，迟到结果不得推进已取消/失租父流程。恢复验证、活动槽位释放、前端及图内恢复继续独立交付。

## 面试表达

“我把取消和截止落实到真正修改演练状态的数据库，而非只在Worker调用前检查。稳定操作只绑定一个不可变窗口，锁等待不会延长期限；取消与应用用同一头锁竞争，成功命令和派发状态同事务落库。响应丢失时查原ID，不换键重发。这个端点仍是本地管理协议，业务审批和Worker授权需要控制面另行接入。”
