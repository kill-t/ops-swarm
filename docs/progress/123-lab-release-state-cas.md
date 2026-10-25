# 123 - 本地发布演练状态与版本CAS

## 目标

在main的d797be7（第122步）上补齐候选执行的发布版本前置能力。先核对AGENTS、交接、Git及现有演练/执行器：原release-500只是内存开关，旧执行器关闭开关后回显target_version，不能据此宣称版本已回滚。

## 设计与边界

以[专项设计](../architecture/123-lab-release-state-cas.md)为事实来源。显式启用的演练模式以PostgreSQL保存order-service的固定版本stable或2026.01.01-rc1，分别对应健康与模拟发布500。响应固定source=local-release-simulation-v1、simulation=true；它不是容器镜像、CI/CD成功或签名部署清单。

CAS比较版本和单调revision，覆盖A→B→A。首次成功命令201，相同完整命令重放200，同键异体或前态冲突409。无变化命令仍需CAS，保留历史但不推进revision；command_count每次首次成功记录递增。重放分别返回历史command、实际current和effect_current，不把旧结果当成当前事实。

默认开关false，旧内存模式及旧执行器保持。新模式中旧开关PUT映射相同固定版本并保存legacy_fault_switch命令，订单、指标和旧GET读取同一持久状态；数据库失败、缺失或损坏均不回退健康缓存。真实runtime-metadata、runtime-inventory、签名及启动身份不改变。管理入口仍属于本机/隔离网络的演练注入接口，不消费可信审批或授予执行资格。

## 实际改动

- 新增fault_lab/release，按严格契约、纯CAS规则、ORM、事务Store及HTTP分离。GET/PUT /__admin/simulated-release提供当前状态及精确幂等结果；只接受固定版本、严格整数修订和有界命令键，拒绝身份、服务、URL或Shell字段。
- 0079_lab_release_state新增lab_release_states当前行与lab_release_commands不可变成功命令。状态仅从stable/1/0初始化；重启使用已有状态，不重置。原生守卫核验正文/hash、前态、时间和相邻序号/修订，命令插入后同事务自动更新状态；直接改写当前状态、命令改写/删除和带数据降级均拒绝。
- 新命令以状态行排他锁串行化；GET以共享锁保证当前行与最新命令来自同一状态版本，避免并发读撕裂。保存点内不比较xmin与顶层事务号，保留嵌套回滚/提交语义。读取核当前行/最新命令及实际JSON/hash，不声称每次遍历全部历史。幂等重放先核完整命令，再返回当前适用性；冲突/非法参数不伪造成功历史，也不冒充可信授权审计。
- 应用生命周期支持注入Store或按环境配置自建Database，启动初始化、失败关闭、退出释放。Compose接好显式OPSSWARM_LAB_RELEASE_ENABLED、数据库地址及control网络，保留127.0.0.1管理端口与只读根文件系统；README和.env.example补可复现启用方式，迁移仍须操作者先执行。
- 测试清理显式覆盖独立演练表，防止跨用例污染；35个历史测试文件同步head，其中通用迁移清理同时增加演练表。未修改已发布迁移、旧审批/核验内容、预算、父图或执行资格。

## 测试方法与结果

验收日期2026-10-25。按JUnit的classname/name去重，共1,976项相关测试通过，采用报告均零失败、零错误、零跳过。子任务先完成22项，主Agent审阅实现与测试后增加2项配置生命周期测试及1项保存点测试，独立重跑全部25项；最终审查另补1项保存点回滚/提交，修正xmin误认顶层事务号导致合法子事务写入被拒绝的问题；不重复累计子任务、早期失败或补跑数量。

| 范围 | 实际结果 |
| --- | --- |
| 全量单元 | 1,905项通过，43.18秒，包含43项新目录/CAS/ABA/严格参数测试 |
| 演练与旧执行器兼容 | 97项通过，4.69秒；其中57项与全量单元重叠，新增覆盖40项旧故障演练API、元数据/清单/证明及默认模式 |
| 最终真实PostgreSQL/API | 25项通过，24.52秒：API10、原生15；重启/多实例并发、完整命令/ABA/noop、旧开关/订单/指标联动、镜像身份保持、真实数据库不可用、写后回滚、损坏拒绝、元数据往返和原生防篡改 |
| 相关旧链路 | 6项通过，31.34秒：通用迁移元数据/完整往返/历史工件保护3项，旧配置/Redis闭环和可信审批API3项 |
| 前置元数据补验 | 既有1项通过，12.42秒，与旧链路重复，不新增计数 |
| 静态与配置 | Ruff通过、825文件格式一致、Mypy477源文件通过；唯一head 0079_lab_release_state；697个本地文档链接有效，Git差异检查通过；docker compose --env-file .env.example config --quiet通过 |

```powershell
.venv/Scripts/pytest.exe tests/unit -q
.venv/Scripts/pytest.exe tests/unit/fault_lab/test_release.py tests/api/test_fault_lab_api.py tests/unit/execution/test_adapter.py -q
.venv/Scripts/pytest.exe tests/api/test_lab_release_api.py tests/integration/test_lab_release_migration.py -q
.venv/Scripts/pytest.exe tests/integration/test_migrations.py tests/api/test_control_plane_api.py::test_database_configuration_plan_runs_through_controlled_verification tests/api/test_control_plane_api.py::test_redis_dependency_plan_runs_through_controlled_verification tests/api/test_operator_identity_api.py::test_authenticated_approval_preserves_idempotency_and_subject_audit -q
.venv/Scripts/ruff.exe check src tests alembic
.venv/Scripts/ruff.exe format --check src tests alembic
.venv/Scripts/mypy.exe src
.venv/Scripts/alembic.exe heads
docker compose --env-file .env.example config --quiet
git diff --check
```

报告位于本地忽略的.pytest_cache/slice123-*.xml，未提交。PostgreSQL17.6由Testcontainers隔离；多实例是真实独立Store/应用共享同库，网络不可用用例连接临时实例中不存在的数据库。未启动或升级用户Compose服务/数据库、调用真实模型、修改真实镜像或连接生产；配置检查不等于完整容器发布验收。output/未读取、未提交。

## 限制与下一步

本片是固定目录的演练应用行为版本，不是部署控制器或真实镜像证明。旧执行器仍没有新候选审批、当前版本与修订的绑定，不能把它回显的target_version当作新执行闭环。新接口没有可信业务主体/审批，未增加Agent工具、候选执行任务/Worker、恢复验证、父运行结算或前端；成功命令历史由数据库保留，尚无分页历史页面。

下一片124推进首个候选专用发布执行链路：先固定精确有效核验批准、原计划/当前版本和CAS修订的绑定，明确持久任务、幂等、租约与失败/迟到路径，再接独立Worker和受限适配器。真实模型用量与原预算不重置；恢复验证、父活动槽位释放与前端继续按路线交付。

## 面试表达

“我先补齐被管系统的版本真值，避免执行器只是关闭故障并回显目标版本。演练版本和真实镜像身份分开，版本加修订号CAS防止ABA。成功命令与当前状态由数据库原子推进，多实例和重启共享同一事实；重放返回原结果，也明确告诉调用方当前状态是否仍匹配。这个能力为后续可信候选执行提供前置条件，本身不代替审批或执行授权。”
