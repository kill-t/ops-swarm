# 125 - 发布操作只读回执与确定性对账

## 目标

在main的735ac2e上补齐第124步明确缺失的按稳定命令键只读查询端口，使后续执行链路能够核对历史命令，避免通过可能产生新副作用的PUT探测结果。继续前已核对AGENTS、交接、进度、123/124设计、Git和实际代码；初始只有用户output/未跟踪目录，未读取或提交该目录。

## 设计与边界

事实来源为[专项设计](../architecture/125-release-receipt-reconciliation.md)。复用原lab_release_commands与lab_release_states，无新表、迁移、任务或业务执行结果。GET /__admin/simulated-release/commands/{idempotency_key}只查询原键，命中200、未命中404；两者均使用完整来源封套，缺失不初始化、不创建命令。禁用、未初始化、存储失败或目标/相邻历史损坏503，不能降级成未执行。

本片将“本操作历史上被应用”和“该效果当前仍适用”明确分开。后续同版本no-op可保持effect_current=true；ABA或后续变更使旧效果失效，原成功命令仍保留。not_recorded只说明查询时无记录，不排除旧写入仍在途，也不授予重发资格。回执是本地演练事实，不是真实镜像或健康证明。

## 实际改动

- 新增fault_lab/release/receipts.py严格封套，完整校验显式service/source/simulation、状态/命令键、规范摘要、序号/修订/时间及历史适用性。拒绝数字布尔、缺少身份后填默认、非严格ISO8601、错误摘要与不可能的修订关联。
- 提取queries.py承载原状态/命令重建及回执读取，store.py保持用例事务。头行FOR SHARE固定状态后读取目标和至多两个相邻命令，沿用头行→命令锁序；不存在查询与并发写入不会被拼成不同版本。只校验目标、邻接与当前头，不声称扫描所有中间历史。纯摘要函数移入policy.py，保留原store.command_hash导入兼容。
- 新增FaultLabReceiptClient，固定本地白名单、GET精确键、总超时及64KiB响应；与旧观测器共用read_transport.py，禁用代理、重定向与压缩，拒绝重复JSON字段及非JSON数值。200/404封套与HTTP状态须一致，通用404不是not_recorded，不重试或回退写入。服务端回执响应Cache-Control:no-store。
- ReceiptObservation保存采集时间与精确路径；纯reconcile_receipt比较调用方提供的完整预期CAS与最早执行时刻，检查30秒观测窗口，返回not_recorded/matched_current/matched_superseded/conflict。未知、错键、错CAS、legacy命令和早于允许时刻的历史不能当作本操作成功。调用方仍须从124完整历史重建预期命令并核对审批/权限。
- README补齐只读接口说明；架构入口、交接与进度同步。第124步prepared、dispatch_eligible=false、原批准/Incident/父运行/预算/活动槽位保持。未接控制面执行结果、独立Worker或图内恢复。

## 测试方法与结果

验收日期2026-10-26。以下报告零失败、零错误、零跳过，按JUnit classname/name去重，共2,123项相关测试通过；专项141项已包含在全量单元中，不重复累计。

| 范围 | 可复现结果 |
| --- | --- |
| 全量单元 | 2,087项，79.79秒，含新增回执/客户端/对账55项 |
| 专项回归 | 141项，4.16秒；新55、旧观测43、原发布规则43，均与全量重复 |
| PostgreSQL/实际ASGI | 36项，215.54秒；新回执API6、旧发布API10、原生迁移15、通用迁移3、第124步双来源实际观测2 |
| 静态 | Ruff通过，847文件格式一致，Mypy492源文件通过；唯一head保持0080_candidate_release_request；719个修改文档本地链接有效，差异检查通过 |

```powershell
.venv/Scripts/pytest.exe tests/unit -q
.venv/Scripts/pytest.exe tests/unit/execution/test_release_receipt.py tests/unit/execution/test_release_observer.py tests/unit/fault_lab/test_release.py -q
.venv/Scripts/pytest.exe tests/api/test_lab_release_receipt_api.py tests/api/test_lab_release_api.py tests/integration/test_lab_release_migration.py tests/integration/test_migrations.py tests/api/test_candidate_release_request_api.py::test_two_sources_bind_actual_lab_get_without_dispatch_or_parent_mutation -q
.venv/Scripts/ruff.exe check src tests alembic
.venv/Scripts/ruff.exe format --check src tests alembic
.venv/Scripts/mypy.exe src
.venv/Scripts/alembic.exe heads
git diff --check
```

新API验证未初始化/禁用503、未知404不写、重启可读、精确历史/no-op/ABA/legacy区别；共享锁读暂停时并发写入阻塞，读先返回旧快照的不存在再允许写入；目标摘要、邻接摘要或缺失邻接均拒绝且不产生写入。实际客户端ASGI仅GET两次，重复读前后数据库快照一致。原迁移往返和第124步双来源链路回归通过。

仅使用隔离Testcontainers PostgreSQL17.6、固定时间和假传输，没有升级用户Compose数据库、调用外部模型或执行候选发布。主Agent完成实现、关键路径核查与统一验收；本轮只读审查子任务因额度未执行，未将其作为验收依据。报告保存在本地忽略的.pytest_cache/slice125-*.xml，未提交。

## 限制与下一步

这是回执端口与纯对账能力，未持久化新的控制面诊断或执行结果；后续Worker必须保存采用的回执和判断。读到旧成功不代表当前恢复成功，读到不存在不能自动重发、换键或延长期限。

下一片126建设候选专用发布受限派发链路：先落实远端派发截止/在途隔离，再接可信执行准入、持久任务、独立Worker及unknown只读收敛。历史授权不能替代届时权限；写出前重验精确批准、原预算/期限与CAS。租约失效、取消及迟到结果均须有确定性边界；本片专项设计中的派发规则仍是待实现契约。恢复验证、父运行收尾、活动槽位释放与前端继续后置。

## 面试表达

“接口超时后，我用原稳定键做独立只读查询，不再用可能执行操作的写接口查历史。回执绑定完整原命令和摘要，同时返回当前修订；因此我能区分本操作曾成功与效果已经被后续操作取代。未知键只表示查询时没有记录，不能排除在途写入，所以它不会自动触发重发；派发期限和租约隔离必须在下一层真正实现。”
