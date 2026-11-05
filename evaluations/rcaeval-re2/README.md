# RCAEval RE2 只读调查试点

后续批次见[第二批证据体积分层评测](batch-02-protocol.md)。

来源为 [RCAEval 作者仓库](https://github.com/phamquiluan/RCAEval)及其 [Hugging Face 数据镜像](https://huggingface.co/datasets/phamquiluan/RCAEval)，查阅于 2026-11-05。样本来自公开微服务故障注入演练。当前选择一个 Online Boutique CPU 故障案例，只取日志和指标。固定修订、原文件路径、大小和 SHA-256 见[下载清单](pilot-manifest.json)。

在项目根目录运行：

```powershell
.venv/Scripts/python.exe evaluations/rcaeval-re2/fetch_pilot.py
```

脚本按文件读取至清单大小加一字节，核对大小、散列和 Parquet 文件标记后写入缓存。缓存命中也执行同样的校验。案例只有两份遥测文件，合计 494,088 字节。

## 接入与对照顺序

1. 先接通一个案例的只读查询和故障服务评分。模型只接收匿名案例 ID、可用数据说明和查询结果。原目录名、故障标签及注入记录留在评分端。两组使用同一观察窗口和同一份可访问证据。
2. 用同一个真实模型做全量证据与按需读取的配对试跑。报告整次调查的输入、输出和总 Token、工具次数、墙钟耗时、故障服务定位和失败原因。工具返回后的额外调用计入总数。供应商用量未知时留空，估算值单列。
3. 试跑后再按服务和故障类型扩样。先核对文件大小和数据使用条款，再下载。调试案例与留出案例分开。关键证据召回需要另行标注；缺少标注时不计算该指标。后续再比较单 Agent、固定并行和动态协作，或验证审查、上下文与经验的增量效果。

逐次结果至少记录代码提交、数据版本、匿名案例 ID、模型及设置、对照组、重复编号、定位结果、失败状态、实际用量和耗时。正式对照保留两组原始指标及差值。见[当前结果](results.md)。

## 首批实验协议

首批固定五个 Online Boutique 案例，覆盖不同服务及 CPU、内存、磁盘、网络延迟和丢包。使用作者发布的故障服务标签，目标是故障服务 Top-1。五个案例是试点样本，未经随机抽样。遥测按[批次清单](batch-01.json)取回，合计 2,346,073 字节。

两组共用固定证据摘要。指标对比数据首尾各 180 秒的均值，日志取末窗口每服务最频繁的两条截断文本和计数。窗口只从指标时间范围计算，评分标签和原始目录名留在程序评分侧。全量组一次获得全部摘要；优化后的按需组先看到服务名和各服务变化比例最大的一项指标，再按白名单读取同源摘要，最多一轮和五个服务条目。多余服务以省略列表反馈，随后关闭工具，要求输出结论。这里的全量指全部摘要。

使用 `deepseek-flash`，关闭思考，温度为 0，每次最多 256 输出 Token。每个案例每组一次，交替组别顺序，无自动重试。通过 [DeepSeek 工具调用接口](https://api-docs.deepseek.com/guides/tool_calls/)进行只读回放。这个独立评测入口验证取证策略，业务 Worker、动态图和审批链各有独立验收。

从项目根目录运行以下命令。输出路径须不存在。密钥及服务地址从项目本地配置读取。

```powershell
uv --no-cache pip install --python .venv/Scripts/python.exe duckdb==1.4.3
.venv/Scripts/python.exe evaluations/rcaeval-re2/run_batch.py .pytest_cache/rcaeval-re2/batch-01-rerun.jsonl
```
