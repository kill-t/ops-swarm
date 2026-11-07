# 执行状态使用一致性只读快照

恢复来源优化后的整栈案例在远端登记后失去首次租约，只读重领得到 `not_applied`，修订 22 保留。该次执行尚未进入恢复采样。代码核查发现，执行状态 GET 重建历史期间持有父级共享锁，与 Worker 的领取及授权写锁冲突。

最小探针暂停状态读取，原路径使另一个事务获取父级写锁超时；使用 PostgreSQL `REPEATABLE READ` 只读快照后，写锁约 0.05 秒取得，完整历史仍可读。执行状态 GET 改用该快照，父关系、来源、任务、尝试与结果在同一快照内校验。写入口使用父锁、当前资格和期限检查。设计见[执行 Worker](../architecture/127-candidate-release-worker.md)。

新增回归在读取来源前暂停 GET，由另一会话提交领取，再验证原读取为一致的 queued 视图，下一次查询为 running。该并发用例与损坏任务或授权的读取、重放、改键及领取拒绝用例均通过真实 PostgreSQL 验证，Ruff 和定向 Mypy 通过。

定向命令选择 `tests/api/test_release_execution_jobs_api.py` 中的 `test_status_snapshot_allows_claim_and_keeps_one_committed_view` 与 `test_corrupt_job_or_authorization_refuses_read_replay_changed_key_and_claim`。完整正常恢复收敛待更新镜像后复验。面试时可用该案例说明一致性快照如何支持查询与 Worker 并发，以及版本校验如何约束后续写入。
