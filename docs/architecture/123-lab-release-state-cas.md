# 本地演练发布状态与版本CAS

## 目标与边界

第122步已有精确核验审批，但旧release-500开关只有内存布尔值，旧执行器回显target_version不能证明版本变化。本片交付显式启用的持久化演练版本状态及CAS，作为候选专用执行的前置能力；不接入候选执行、审批消费、Worker、真实部署或图恢复。

状态明确标识source=local-release-simulation-v1、simulation=true，仅order-service和固定版本stable、2026.01.01-rc1；stable健康、rc1模拟发布500。它是演练应用的行为状态，不是部署平台的镜像身份。runtime-metadata、runtime-inventory及其签名、启动身份、静态日志标签均保持原边界，不通过管理接口伪造镜像或签名。

默认OPSSWARM_LAB_RELEASE_ENABLED=false，旧进程内行为及旧执行器保持。启用后订单/指标/旧release-500 GET均读取同一持久状态；旧PUT enabled映射固定版本并记录legacy_fault_switch命令，不能绕过修订推进。新CAS回滚stable会改变演练订单行为；不能据此宣称真实发布回滚。数据库不可用、状态缺失或损坏时503或启动失败，不回退默认健康/冻结数据。

## 状态与接口

GET /__admin/simulated-release返回service_name、source、simulation、version、revision、command_count、release_500_enabled、updated_at。首次显式启动初始化stable/revision=1/count=0，重启只读取已有状态，不重置。revision仅在版本真实变化时递增；command_count每个首次成功命令递增，避免无变化命令覆盖最新审计却无法排序。

PUT同路径接受严格expected_version、expected_revision（1..2147483647整数）、target_version及8..100字符idempotency_key，不接受身份、URL、Shell、服务或未知版本。先精确主体服务锁下查幂等键，再查CAS；同键同完整命令重放200，不同命令409，旧修订或版本冲突409。首次成功命令201；目标等于当前时仍须匹配CAS，保留无变化命令，不推进版本revision。

响应包含applied（首次记录与重放）、不可变command、current（本次读取时的真实状态）及effect_current。重放不得改变状态或续写命令；历史结果和当前状态分开表达，effect_current要求版本和revision均仍匹配原结果。A→B→A后旧修订无效。旧PUT不要求客户端幂等键，每次分配新键，保留原HTTP响应结构；相同enabled不推进revision，但保留命令历史。

成功命令才进入发布历史；CAS/幂等冲突返回409，不追加成功记录，参数错误422。它们没有可信业务主体，不是控制面的授权或拒绝审计。

这些是绑定本机/隔离网络的演练管理接口，与原故障注入管理入口相同，不是可信业务审批入口，不授予执行资格。未来候选执行须在控制面重新校验审批、权限、预算、租约、幂等和当前版本，再由固定适配器调用；本片不开放Agent工具。

## 持久化与失败路径

0079_lab_release_state新增lab_release_states单服务当前行和lab_release_commands不可变成功命令。当前行保存版本、单调revision/count、最新命令及UTC时间；命令保存种类cas/legacy_fault_switch、完整规范JSON/hash、前态/后态、序号及UTC时间。初始化不虚构操作或审批。

应用用例以SELECT FOR UPDATE串行化同服务修改，数据库触发器再次核验固定目录、命令结构/hash、前态、相邻序号/修订与时间。插入命令后在同事务自动更新当前行；当前行仅由命令的自动推进触发路径更新，并精确匹配相邻前态/后态；保存点内不以行xmin等于顶层事务号作判断，不能单改状态、伪造前态或改写/删除命令。存在新状态或命令时拒绝迁移降级。GET以共享状态行锁保持头行和最新命令一致，只读且不初始化；读取验证当前行与最新命令以及命令实际JSON/hash的一致性，不宣称每次读取重新审查全部历史命令。

Store及HTTP异步访问PostgreSQL，禁止同步网络阻塞。应用生命周期初始化/释放自有Database，测试可注入Store；Compose传入显式开关和数据库地址，并为启用后的访问接入control网络，仍只绑定127.0.0.1管理端口，不引入Docker Socket。开启前须先执行正式迁移，未升级schema直接启动失败；不自动升级用户数据库。

## 验收与后续

覆盖纯CAS/严格参数/ABA、真实PostgreSQL元数据/空迁移往返、重启与多实例共享、同键/不同键并发、历史重放与当前漂移、无变化、旧开关联动、订单/指标一致、写入失败及保存点回滚/提交、原生防篡改/同事务、数据库缺失失败关闭及真实镜像身份不改。保留旧故障演练、预检和执行器回归，使用假模型，不连接生产。

随后在精确核验批准和本片版本CAS之上建立候选专用执行任务、租约与结果审计；恢复验证、父运行结算、活动槽位释放和前端仍后置。
