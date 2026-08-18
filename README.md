# OpsSwarm

当线上系统报警后，OpsSwarm 自动拉取日志、指标和最近变更，组织多个 Agent 并行调查，输出带证据的根因判断和修复方案；经工程师审批后执行受控操作，并验证系统是否恢复。

## 当前状态

项目目前处于“项目章程与架构设计”阶段，尚未生成业务代码，也不能启动服务。当前重点是先固定产品边界、可靠性原则、技术栈和数据模型，再按小功能切片逐步实现。

## 为什么做 OpsSwarm

OpsSwarm 面向已经使用 Docker/Kubernetes、Prometheus/Grafana、Loki/ELK 和 GitHub/GitLab，但缺少成熟 SRE 平台的中小型研发团队。它希望解决三个问题：

- 缩短从告警到恢复的时间，降低 MTTR。
- 把分散的日志、指标和变更整理成可验证的事故证据。
- 通过风险分级、人工审批和白名单执行避免 LLM 误操作。

OpsSwarm 与原 Enterprise AI Assistant 的边界明确：原项目聚焦文档摄取、检索、引用与 RAG 评测；本项目聚焦实时工具调用、状态化 Agent 编排、安全执行、失败恢复和 Agent 评测。

## 核心流程

```text
告警 → Incident → 调查任务 → Evidence Board → 证据质疑与验证
     → 根因与处置计划 → 人工审批 → 白名单执行 → 恢复验证 → 事故报告
```

## 架构概览

控制面采用模块化单体：FastAPI API 和独立 Worker 共享领域代码及业务数据库；故障演练环境作为独立被管系统。LangChain 提供模型与工具抽象，LangGraph 负责需要持久化、并行、循环和人工暂停的 Agent 工作流。

完整设计见：

- [项目章程](docs/architecture/01-project-charter.md)
- [系统架构](docs/architecture/02-system-architecture.md)
- [技术栈选型](docs/architecture/03-technology-stack.md)
- [数据库设计](docs/architecture/04-database-design.md)
- [开发进度与复盘](docs/progress/README.md)

## 实施路线

1. 项目章程、架构边界和最小目录。
2. 确定性故障模拟服务与 Incident 状态机，不接 LLM。
3. 单 Agent 调查基线。
4. 协调 Agent 与日志、指标、变更并行调查节点。
5. Evidence Board、质疑节点和结构化处置计划。
6. 人工审批、白名单执行器和幂等操作。
7. 恢复验证、失败恢复与时间线 UI。
8. 评测集以及单 Agent、多 Agent 对比。
9. 接入真实 Prometheus、Docker、GitHub 或 MCP 工具。

第一个可运行切片将使用“新版本触发大量 500”场景，先打通故障注入、告警入库、Incident 去重、状态迁移和不可变时间线。

## 安全声明

第一版只用于本地 Docker Compose 故障演练，不可直接部署到生产环境。LLM 不拥有任意 Shell 或生产写权限；所有实际操作必须由确定性白名单执行器完成，高风险动作必须经过人工审批。
