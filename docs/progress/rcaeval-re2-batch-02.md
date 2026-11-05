# RCAEval RE2 第二批分层评测

2026-11-05。对 6 个新案例分别构造紧凑与扩展证据，保留同一案例的故障标签及观察窗口。用真实 DeepSeek Flash 完成 24 次配对调查。文件校验、数据解析、阈值分层、Ruff 与差异检查通过。见[协议](../../evaluations/rcaeval-re2/protocols/batch-02-protocol.md)。

按需取证在约 4 KB 摘要上增加 Token，在约 20 KB 扩展证据上减少 Token，两个版本均比全量少定位对一例。预设分流规则通过逐次结果离线计算，尚未用于业务工作流。见[实测报告](../../evaluations/rcaeval-re2/reports/results.md#第二批证据体积分层结果)。

面试可说明，同一优化在不同证据体积下可能方向相反，应同时报告定位质量、整次调用量和时延，再决定部署策略。
