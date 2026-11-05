# RCAEval RE2 十五例验证与停止线

2026-11-05。在运行前固定完成率、定位准确率、总 Token 和耗时四项门槛。复用长证据方案，在 15 个新案例上完成 30 次真实 DeepSeek Flash 调查。数据文件、留出案例、取证范围记录、Ruff 和差异检查通过。见[协议](../../evaluations/rcaeval-re2/protocols/batch-03-protocol.md)。

四项门槛通过，取证优化按计划收口。唯一误判中工具已返回目标服务证据，仍保留诊断限制。下一方向为单 Agent 与动态多 Agent 的同题对照。见[实测结果](../../evaluations/rcaeval-re2/reports/results.md#第三批十五例留出验证与收口)。

面试中可以说明，先定停止线再看结果，并同时保留 Token 收益、诊断损失与时延代价，避免为了一个指标不断试调。
