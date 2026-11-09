# 正式恢复脚本的中断恢复与事故收敛

2026-11-09。把正式脚本的验证从有效发布准备推进到事故收敛。报告新增准备记录及有效期、执行操作及尝试、采样尝试和收敛内容哈希。准备记录失效时停止提交执行任务。控制面拒绝时保留格式受限的机器错误码，便于核对持久命令审计。[演示设计](../architecture/local-demo-reproduction.md)

隔离 Linux 中使用真实 PostgreSQL 和当前迁移。正式脚本访问进程内控制 API 与订单服务，调查使用脚本模型和固定取证。首次在创建执行任务时收到 HTTP 409，未派发 CAS。当时只有 HTTP 状态，具体拒绝原因未确认。补充报告后复测通过，原审批、派发期限和租约策略沿用现有规则。

测试在订单 CAS 已生效、执行结果尚未落库时中断原 Worker，等原租约自然到期后创建接手实例。重复提交复用同一执行任务。接手实例只对原操作发出一次 GET，订单修订只增一。真实恢复 Worker 保存同一次尝试的三个 HTTP 200，事故进入 `resolved`、调查运行进入 `settled`，收敛记录与 PostgreSQL 都确认槽位已释放。

`python -m pytest tests/unit/test_local_demo.py` 通过 26 项，`python -m pytest tests/api/test_live_human_continuation_api.py -k demo_recovery` 通过一项隔离 PostgreSQL 集成测试。Ruff、格式和差异检查通过。该测试覆盖进程内应用与 Worker 实例接手。当前正式 Compose 的三个案例和保留卷重启仍需在时钟稳定的环境补验，本轮沿用已有时钟预检结论。[正式演示入口](../local-demo.md)

面试时可以沿同一个操作 ID 解释两次执行尝试。远端写入和本地记账之间发生中断，接手实例先读取原回执，再由业务订单采样确认恢复，由独立收敛步骤释放事故槽位。
