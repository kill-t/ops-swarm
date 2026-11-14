# OpsSwarm

OpsSwarm 是一个基于 Python 的多 Agent 事故响应系统，用于在本地 Docker Compose 环境演练订单服务故障。它接收告警，组织日志、指标和变更调查，在人工审批后执行受控回滚，并通过订单接口的实测响应判断服务是否恢复。工程师可在 Web 协作台查看证据、补充信息和处理审批。

## 核心功能

- **多 Agent 调查**。协调 Agent 根据事故信息分配专业任务，调查结果附带数据来源和时间窗口。审查 Agent 可以根据证据缺口提出补查，各轮调查共享调用次数、Token、费用和时限预算。
- **人工协作与恢复**。系统将调查任务、证据和人工决定写入 PostgreSQL，使用 LangGraph checkpoint 恢复中断的工作流。进入人工等待时释放 Worker，工程师提交补充信息后可触发重新评估。
- **受控回滚与验证**。审批绑定具体方案和核验依据。执行器仅调用本地演练环境的白名单操作，重复提交复用原操作。系统用 HTTP 探针确认恢复，明确未恢复时可在原预算内再调查一次并转交工程师。

## 本地运行

### 1. 安装工具

以下命令以 Windows PowerShell 为例。安装并启动 Docker Desktop，使用 Linux 容器，确认 Docker Compose 为 **2.24.4 或更高版本**。首次构建需要联网拉取容器镜像并安装依赖。[Docker Desktop 安装指南](https://docs.docker.com/desktop/setup/install/windows-install/) · [Compose 版本要求](https://docs.docker.com/reference/compose-file/merge/#replace-value)

尚未安装 Git 或 uv 时，可用 WinGet 安装所缺工具。安装后重新打开 PowerShell。[Git 安装指南](https://git-scm.com/install/windows) · [uv 安装指南](https://docs.astral.sh/uv/getting-started/installation/)

```powershell
winget install --id Git.Git -e --source winget
winget install --id astral-sh.uv -e
```

确认命令可用，并检查 Docker 服务端是否已启动。

```powershell
git --version
uv --version
docker version
docker compose version
```

### 2. 克隆仓库并安装 Python 依赖

```powershell
git clone https://github.com/kill-t/ops-swarm.git
cd ops-swarm
uv python install 3.12
uv sync --locked
```

`uv sync --locked` 按仓库的锁文件安装依赖，并创建项目虚拟环境 `.venv`。

### 3. 启动演练服务并预检

```powershell
.\scripts\local-demo.ps1 -Action Start
.\scripts\local-demo.ps1 -Action Check
```

首次启动会构建应用镜像、初始化数据库和 checkpoint，并生成本地演示身份。演示使用仓库内的脚本模型，无需配置 `.env` 或模型 API 密钥。数据库、监控和前端由 Compose 启动。预检检查服务、身份、镜像与数据库迁移版本以及容器 UTC 时钟。

| 服务 | 本机地址 |
| --- | --- |
| 事故协作台 | <http://127.0.0.1:35173> |
| 控制面健康检查 | <http://127.0.0.1:38080/health> |

### 4. 运行场景并停止服务

预检通过后，按需选择下列一条场景命令。每次运行都会创建一个新事故。

```powershell
.\scripts\local-demo.ps1 -Action Run -Scenario handoff
.\scripts\local-demo.ps1 -Action Run -Scenario recovery
.\scripts\local-demo.ps1 -Action Run -Scenario reflow
```

| 场景 | 演练内容 |
| --- | --- |
| `handoff` | 用固定数据演练多轮补查、重启后读回人工等待记录，以及提交补充材料后的重新评估 |
| `recovery` | 演练人工审批、单次回滚和订单恢复验证 |
| `reflow` | 演练回滚后仍未恢复时的新一轮取证、独立复查和人工接管 |

```powershell
.\scripts\local-demo.ps1 -Action Status
.\scripts\local-demo.ps1 -Action Stop
```

停止服务会保留数据卷。演示手册包含报告位置和登录方式。[本地演示手册](docs/local-demo.md) · [自配模型与身份](docs/local-release.md)

## 系统架构

| 层 | 职责 |
| --- | --- |
| Web 与控制面 | Vue 提供事故协作台，FastAPI 提供业务 API 和可重放的 SSE 时间线 |
| 调查 Worker | LangGraph 编排任务分派、并行调查和补查，LangChain 负责模型适配、工具调用和结构化输出 |
| 数据与取证 | PostgreSQL 保存业务状态、任务和审计记录，日志与指标通过只读 MCP 服务查询，变更通过独立适配器查询 |
| 演练与处置 | Docker Compose 运行订单故障环境，独立 Worker 消费执行与恢复验证任务 |

## 代码入口

- 控制面和 API。[控制面代码](src/opsswarm/control_plane) · [API 设计](docs/architecture/05-api-design.md)
- Agent 分工、上下文和共享预算。[调查模块](src/opsswarm/investigation) · [Agent 系统设计](docs/architecture/65-agent-system-design.md)
- 本地故障、回滚和恢复验证。[故障演练](src/opsswarm/fault_lab) · [执行模块](src/opsswarm/execution) · [恢复验证模块](src/opsswarm/verification)
