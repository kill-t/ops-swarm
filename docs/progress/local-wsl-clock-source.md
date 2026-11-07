# WSL 周期倒退与时间同步来源核查

来源遍历复用已提交为 `9b7b022`。常规 Dockerfile 的第一次构建遇到镜像仓库 TLS 超时，限定重试成功；镜像内两处来源代码及恢复 Worker 与工作区哈希一致。标准 Compose 装配恢复通过，执行和恢复 Worker 均使用原模块命令，无诊断挂载，原卷保留。未创建新事故、续用过期授权或改写修订 40 的失败事实。

启动探针再次捕获 UTC 倒退。整栈就绪后的持续探针也在空闲阶段反复捕获负 UTC 增量，同期单调时钟持续向前；数据库活动查询数为零。倒退不只出现在启动或发布来源重建期间，不能把此前 WSL 重置视为时钟修复。详细探针与资源记录保存在忽略缓存。

只读核查确认 Ubuntu 22.04 的 `systemd-timesyncd` 已启用且运行，使用 `ntp.ubuntu.com`，轮询间隔 32 秒。NTP 报文目的时刻与探针倒退时刻对应；WSL 使用 `hyperv_clocksource_tsc_page`。这形成双重校时冲突的候选解释，尚未通过停止服务的对照确认，不认定它解释全部执行迟滞或 141 欠项。

查阅于 2026-11-08：[Ubuntu 官方时间同步说明](https://ubuntu.com/wsl/docs/stable/explanation/time-sync/)指出 Ubuntu 24.04 及之前默认启用的时间同步服务可能与 Hyper-V 隐式校时冲突；[Microsoft WSL 架构说明](https://learn.microsoft.com/en-us/windows/wsl/about)说明 WSL 2 发行版共享内核。外部资料只用于诊断假设与受控对照，不作为本机根因证明或项目已实现能力。

用户随后明确授权临时停止 Ubuntu 22.04 的时间同步服务，在同一标准栈对照；不修改启用状态或配置，不停止发行版或 Docker。实际执行命令为：

```powershell
wsl --distribution Ubuntu-22.04 --user root --exec systemctl stop systemd-timesyncd.service
```

停止前四分钟捕获 16 次负 UTC 增量，停止后四分钟仍捕获 8 次；另一个 Windows 四分钟探针没有负增量或超过一百毫秒的跳变，其窗口覆盖停服务和恢复后的部分时段。停服务没有消除异常，因此不能认定该服务是唯一原因，更不能据此宣称平台时钟已修复。另以 `adjtimex` 的零模式只读查询记录内核时钟参数，并比较原始与调整后时钟，尚未定位剩余调整来源。查询边界依据 [Linux 接口说明](https://man7.org/linux/man-pages/man2/adjtimex.2.html)，未设置频率、tick 或时钟源。WSL 系统发行版的进一步只读查询返回 `WSAETIMEDOUT`，不继续扩大平台操作。

对照后已将命令改为 `start` 恢复服务，并独立确认 `active/running/enabled`。标准栈保留，前端、控制 API 与演练服务健康检查均为 200。用户未跟踪目录保持未触及，诊断脚本与日志仅保存于忽略缓存。

下一项仍是正常恢复、事故收敛与槽位释放。所选 WSL 环境的连续 UTC 尚不满足验收前提，先恢复稳定校时环境，再以新案例继续；不重复整链，不将 Linux 组件用例成功当作完整 Compose 发布通过，也不自动调整共享内核或系统配置。边界见[交付设计第 7 节](../architecture/local-multi-agent-release-scope.md)。

面试时可说明如何用空闲期最小探针区分应用迟滞与系统时钟异常，并将已验收代码、环境诊断与尚待对照的根因假设分开。
