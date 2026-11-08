# OpsSwarm

OpsSwarm 是面向订单服务事故演练的本地多 Agent 项目：从告警与现场证据出发，动态组织专业调查和独立审查，经人工补充、可信审批与白名单执行，验证恢复结果；明确未恢复时，在原预算内再调查一次并交由工程师接管。

项目使用 LangGraph、LangChain、FastAPI、PostgreSQL 和 Vue。控制面采用模块化单体，长任务由独立 Worker 消费；只操作隔离的本地 Docker Compose 演练环境。

## 当前交付状态

| 范围 | 当前事实 |
| --- | --- |
| 动态调查 | 现场 graph-v16 已接入专业分工、精确窗口取证、多轮综合、独立审查和持久交接 |
| 上下文与共享预算 | 任务材料清单、工作摘要、证据引用、累计调用/Token/费用及期限检查已有实现；首轮协调可消费同范围可信经验 |
| 人工续接与处置 | 可信补充、实际重评、候选规划、条件核验、审批、单次演练回滚及恢复采样已有组件与代表链验收 |
| 一次失败回流 | 隔离 Compose 代表场景已完成新窗口取证、综合、独立复查和持久人工接管，保留原账本及唯一处置 |
| 界面与部署 | 最小可信协作台及独立 Compose 入口已有指定验收；本版完整正常恢复、事故收敛与槽位释放仍待最终整栈验收 |

历史 MVP 已完成当时的发布验收，不代表当前现场多 Agent 版本已完整发布。WSL 时钟异常和 141 的原 API 欠项单列，不能把它们全部归为同一原因；无需整栈的代码整改可继续。具体证据和继续位置见[项目交接](docs/PROJECT_STATE.md)与[进度索引](docs/progress/README.md)。

## 业务主线

```text
告警落库 → 动态专业调查与补查 → 综合与独立审查
         → 人工补充及实际重评 → 候选与确定性规划
         → 条件核验与可信审批 → 单次白名单处置 → 客观恢复验证
           ├─ 恢复：事故收敛、槽位释放与复盘
           └─ 未恢复：原预算内一次再调查、独立复查与持久接管
```

模型提出任务、结论和方案；确定性代码负责权限、预算、版本、状态迁移及执行资格。业务事实与审计保存于 PostgreSQL，LangGraph checkpoint 保存工作流恢复位置，不能替代业务事实。经验辅助调查，不能充当当前证据或执行授权。

本版限定已有专业角色、一种受支持的单步骤处置和一次失败回流。通用多步骤、回流后的再次自动执行、生产部署及更多平台接入属于后续版本，见[有限交付范围](docs/architecture/local-multi-agent-release-scope.md)。

## 本地运行

工程链演示使用仓库内脚本模型，可自动生成独立身份和隔离配置。先安装依赖并启动 Docker Desktop Linux 引擎，按[演示手册](docs/local-demo.md)运行：

```powershell
uv sync --locked
.\scripts\local-demo.ps1 -Action Start
.\scripts\local-demo.ps1 -Action Check
.\scripts\local-demo.ps1 -Action Run -Scenario recovery
.\scripts\local-demo.ps1 -Action Stop
```

入口使用独立项目名 `opsswarm-local-demo` 和数据卷，支持人工交接、正常恢复、未恢复后一次回流；`Stop` 保留数据。预检失败时不开始业务案例。脚本模型结果只代表工程链验证。需要自配模型时使用[标准发布手册](docs/local-release.md)及项目 `opsswarm-local-release`；模型调用可能产生实际费用，需另行安排。两个项目共用以下端口，不能同时启动。

| 入口 | 本机地址 |
| --- | --- |
| 协作台 | <http://127.0.0.1:35173> |
| 控制 API 健康检查 | <http://127.0.0.1:38080/health> |
| 订单演练服务 | <http://127.0.0.1:38001> |
| 变更模拟服务 | <http://127.0.0.1:38002> |

数据库、缓存及遥测留在容器网络。演练前检查 UTC 连续性和凭据有效期；不能以放宽租约或审批窗口换取验收成功。单 Agent、评测、历史 MVP 与独立 Worker 的按需命令见[手工运行与历史接口参考](docs/manual-reference.md)。

## 代码入口与兼容边界

| 职责 | 入口 |
| --- | --- |
| 控制面协议与装配 | [control_plane/app.py](src/opsswarm/control_plane/app.py) |
| 当前 v15/v16 图装配 | [worker/dynamic_factory.py](src/opsswarm/worker/dynamic_factory.py) |
| 动态调查与 checkpoint | [dynamic_third_round_routing_graph.py](src/opsswarm/investigation/dynamic_third_round_routing_graph.py) |
| 专业规划端口 | [coordination/ports.py](src/opsswarm/investigation/coordination/ports.py) |
| 受控执行与恢复 | [执行 Worker](src/opsswarm/execution/release_jobs/worker.py)、[恢复 Worker](src/opsswarm/verification/candidate/worker.py) |
| 前端事故页面 | [IncidentWorkroomView.vue](web/src/views/IncidentWorkroomView.vue) |

v1/v2、冻结 v3 graph-v15、历史数据集和评测结果保留。旧 v3 graph-v1—v14 保留原拓扑与恢复契约，历史工厂共用冻结依赖装配；默认动态 Worker 只领取当前 v15/v16，不自动升级旧 checkpoint。代码退役依据运行与恢复支持范围，不能仅凭文件年代或测试引用决定。

## 开发与验证

依赖由 `uv.lock` 固定。按改动选择测试与静态检查，例如：

```powershell
uv sync --locked
uv run pytest tests/unit/investigation/test_dynamic_factory.py
```

业务状态、权限和预算优先用确定性单元测试；事务、原生约束和持久恢复使用真实 PostgreSQL。相关检查通过后不重复整批运行；同根因两次长链失败先转最小探针。模型测试使用假模型和固定工具响应，不把外部模型调用作为默认 CI 条件。协作、目录与提交规范见 [AGENTS.md](AGENTS.md)。

## 进一步阅读

- [当前范围与发布终点](docs/architecture/local-multi-agent-release-scope.md)
- [现场动态协作设计](docs/architecture/live-dynamic-investigation.md)
- [Agent、记忆、上下文与 Harness 设计](docs/architecture/65-agent-system-design.md)
- [长期路线](docs/architecture/64-full-multi-agent-roadmap.md)
- [项目状态与代码导航](docs/PROJECT_STATE.md)
- [历史进度与验收记录](docs/progress/README.md)
- [手工命令与历史接口](docs/manual-reference.md)
