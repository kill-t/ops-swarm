# 51 - 多实例运行清单与滚动发布判定

## 目标

把第 50 步的单实例运行核对扩展为有界实例集合，并严格区分全部匹配、受控滚动发布、部分漂移和全部不匹配。滚动发布不能由模型猜测，也不能仅凭两个 digest 并存自动放行。

## 设计与边界

- 保留单实例端点兼容旧 Evidence，新增默认关闭的 `GET /api/v1/runtime-inventory`。
- 当前进程身份仍使用原四个环境变量；可选严格 JSON 只注入其他实例和发布窗口。
- 清单最多 64 个实例、注入 JSON 最长 64 KiB、发布窗口最长 30 分钟；UUID、仓库、digest、时区和启动时间都由 Pydantic 失败关闭校验。
- 只有“最新 artifact + 紧邻上一 artifact + progressing + 目标一致 + 窗口有效 + 无未知实例”才是 `rolling_update`。
- 稳定期混跑、窗口过期、发布目标错误、更早历史 digest 或未知 digest 都不会被视为正常滚动发布。
- 前端重新计算实例分类、计数与窗口，不盲信 Evidence 中的状态字符串。
- 清单由本地部署环境模拟注入，不代表 Kubernetes 实时服务发现、实例存活或签名运行证明。

## 实际改动

- 新增运行实例、发布窗口、注入声明、清单响应和精确禁用响应模型。
- 订单服务增加运行清单端点，启动时验证当前实例、附加实例和时间线。
- Compose 和 `.env.example` 增加可选清单声明变量，没有增加 Socket 或额外权限。
- 变更调查改为一次读取运行清单，选取最新与紧邻上一部署并确定性聚合状态。
- Evidence 增加实例列表、三类计数、发布阶段、窗口、目标和上一 artifact 字段。
- 作战室增加多实例摘要和逐实例列表；匹配、上一版本和非预期实例使用不同状态颜色。
- 保留第 50 步单实例 Evidence 的展示兼容性。

## 测试方法与结果

执行完整后端质量门禁：

```powershell
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run pytest
```

结果：352 个文件格式检查通过，Ruff 无问题，mypy 检查 143 个源码文件无问题，后端 `552 passed`。提交前补充 30 分钟窗口护栏后的第一次完整复跑中，Docker Desktop Linux Engine 意外停止，425 个无数据库用例通过、127 个 Testcontainers 用例统一因 Docker 命名管道不存在而在准备阶段报错；重启 Engine 后使用原命令完整重跑通过，没有忽略测试或修改断言。

执行完整前端质量门禁：

```powershell
cd web
npm run typecheck
npm test -- --maxWorkers=1
npm run build
```

结果：Vue/TypeScript 类型检查通过，9 个测试文件中的 34 个测试全部通过，Vite 生产构建成功。

执行配置和格式验收：

```powershell
docker compose config --quiet
git diff --check
```

结果：Compose 配置有效，Git 差异没有空白错误。Git 只提示 Windows 工作区未来可能进行 LF/CRLF 转换，不是内容错误。

真实 Compose 验收中构建当前 `ops-swarm-runtime:local`，注入随机的当前实例 UUID、附加实例 UUID、实际构建 digest、上一 digest 和十分钟发布窗口。`runtime-inventory` 精确返回 2 个实例、`progressing` 和完整目标 digest；订单服务与现场 Worker 都引用刚构建镜像、使用只读根文件系统，Docker Socket 挂载数为 0。订单接口返回成功，版本故障关闭、数据库池为 `50`、Redis 可用。

现场调查适配器随后读取真实变更服务和该运行清单。现有最新部署事实声明的仓库/digest 与本次临时运行清单都不相同，因此结果正确聚合为 `mismatched`：2 个实例、目标匹配 0、上一版本 0、非预期 2。合法 `rolling_update` 以及稳定期混跑、窗口过期、目标错误、更早历史 digest 等分支由固定单元测试覆盖。本步没有调用模型 API，也没有读取或输出 `.env` 密钥。

## 限制与下一步

- 本地清单是进程返回的部署环境声明，不探测其他实例是否真实存活或 Ready。
- 发布声明没有签名；控制部署环境的主体可以同时伪造实例与窗口。
- 当前不聚合流量权重、可用区、ReplicaSet、健康探针或 Pod 重启次数。
- 下一步应设计最小权限部署控制器的签名运行证明，再决定接本地签名模拟器还是 Kubernetes 只读适配器。

## 面试表达

> 我没有为了兼容滚动发布就简单允许两个版本，而是把最新部署、紧邻上一部署和显式发布窗口一起校验。所有实例先按完整仓库和 digest 分类，只有目标与上一版本混合、没有第三种版本、窗口有效且控制面目标一致时才判定 rolling_update；否则是部分或全部不一致。响应有大小、新鲜度、UUID 和时间线护栏，页面还会独立重算。Agent 不拥有 Docker Socket。当前清单只是本地声明，生产化需要最小权限、带签名的部署控制器。
