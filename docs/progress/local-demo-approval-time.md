# 正式演示的核验时间与发布准备

2026-11-09 Docker 启动后，在本地既有镜像中做了一次 35 秒空载探针，捕获 4 次 UTC 倒退和 4 次超过 100ms 的差异。当前环境继续阻断正式 Compose 场景。探针结果保存在忽略目录 `.pytest_cache/local-demo-recovery-preflight.log`。[已有环境记录](local-wsl-clock-source.md)

正式脚本此前用 Windows 时间声明人工观察，控制面按自己的时钟核验批准前后边界。现在使用订单失败响应的 UTC `Date`，核对实际版本、修订与 HTTP 500。秒级精度导致时间早于批准时只补做 GET，累计最多三次。缺失、无效或持续过早的响应会停止核验，报告保留实际观察时间及核验 ID。原批准和观察时效由现有 API 校验。[演示设计](../architecture/local-demo-reproduction.md)

23 项演示脚本测试通过。`uv run pytest tests/api/test_live_human_continuation_api.py -k demo_approval` 在隔离 PostgreSQL 中以正式脚本走完草案、可信审批、条件核验、核验审批和发布准备。测试使用进程内订单服务、固定取证和夹具生成的服务端时间头，读取到当前有效的准备记录。Ruff、格式及差异检查通过。真实 Compose 派发、三次业务探针、事故收敛及槽位释放仍待验收。

面试时可以说明观察时间来自实际 HTTP 响应，控制面仍核对它与批准的因果顺序。用缺失、错误和过早时间的拒绝回归验证这一边界，再用真实数据库验证发布准备仍绑定原审批和版本。
