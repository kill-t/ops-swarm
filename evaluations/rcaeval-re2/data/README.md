# 原始遥测数据

`raw/<case_id>/` 是本地可复现下载目录。每个案例有 `logs.parquet`（原始日志）和 `metrics.parquet`（原始指标）。目前三批分别有 5、6、15 个不同案例。另有下载试点 `re2-pilot-001`，与 `re2-b01-01` 来自同一个公开案例。

原始文件由 [cases](../cases/) 中的固定版本和 SHA-256 清单校验，`raw/` 不纳入 Git。下载与回放命令见[评测说明](../README.md)。逐次模型结果在 [results](../results/)，汇总和对照见[报告](../reports/results.md)。
