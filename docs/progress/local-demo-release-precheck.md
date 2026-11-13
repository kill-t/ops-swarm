# 正式发布的环境预检与保留卷核对

2026-11-13 在 `e2a4abd` 上启动已停止的 Docker Desktop。引擎就绪后用既有 Linux 镜像和正式脚本中的 `CLOCK_PROBE` 做了一次无网络的 35 秒探针。UTC 倒退为 0，墙钟相对单调时钟偏差超过 100 毫秒有 2 次。环境尚未满足发布验收条件，三个正式场景未开始。探针原始结果保存在 `.pytest_cache/local-demo/release-20261113/clock-precheck.log`。[演示设计](../architecture/local-demo-reproduction.md)

正式入口用新目录生成身份配置，合并 Compose 配置通过 `config --quiet`。只启动隔离演示项目的 `control-db`，用 `docker inspect` 确认数据库挂载 `opsswarm-local-demo_control-db-data`，并在只读事务中查询 PostgreSQL 系统标识、迁移版本和记录数。`stop control-db` 后重新 `up --no-deps --no-build --wait control-db`，原卷、系统标识和迁移 `0100_reflow_input_projection` 均一致。事故和 checkpoint 数量均为零。数据库核对已通过，完整案例的重启后读回待补验。数据库已停止并保留卷。

演示手册补齐三个案例通过后的重启读回步骤。面试时可以用 PostgreSQL 系统标识证明使用了原数据库，再按报告中的事故、调查与操作身份展示实际业务状态仍能恢复。后者需要完成三个案例后验证。[演示手册](../local-demo.md)
