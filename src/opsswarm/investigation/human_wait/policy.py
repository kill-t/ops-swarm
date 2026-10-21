"""等待命令的确定性业务规则，不驱动工作流或授权动作。"""

from datetime import datetime

from opsswarm.investigation.context.hashing import content_hash
from opsswarm.investigation.handoff.contracts import TerminalHandoffRecord
from opsswarm.investigation.human_wait.contracts import (
    MAX_SUPPLEMENTS,
    HumanWaitError,
    HumanWaitEventRecord,
    HumanWaitState,
    SupplementWaitCommand,
    WaitCommandBase,
)


def evaluate_wait_command(
    handoff: TerminalHandoffRecord,
    state: HumanWaitState,
    command: WaitCommandBase,
    *,
    subject_id: str,
    existing: HumanWaitEventRecord | None,
    now: datetime,
) -> str:
    if existing is not None:
        if existing.subject_id != subject_id or existing.command_sha256 != content_hash(command):
            raise HumanWaitError("human_wait_idempotency_conflict", "幂等键已绑定另一完整命令")
        return "replay"
    if handoff.destination != "await_human":
        raise HumanWaitError("human_wait_not_available", "运行未交接至人工等待")
    if command.handoff_id != handoff.id or command.source_sha256 != handoff.source_sha256:
        raise HumanWaitError("human_wait_source_conflict", "命令不匹配精确人工交接来源")
    if state.status != "waiting":
        raise HumanWaitError("human_wait_cancelled", "本次自动调查已经取消")
    if command.expected_wait_version != state.state_version:
        raise HumanWaitError("human_wait_version_conflict", "人工等待版本已变化")
    if now < handoff.created_at or (state.events and now < state.events[-1].created_at):
        raise HumanWaitError("human_wait_time_conflict", "人工命令不能早于已保存事实")
    if isinstance(command, SupplementWaitCommand):
        if len(state.events) >= MAX_SUPPLEMENTS:
            raise HumanWaitError("human_wait_input_limit", "人工补充已达到上限，仍可取消")
        if any(item.observed_at is not None and item.observed_at > now for item in command.items):
            raise HumanWaitError("human_wait_future_observation", "人工陈述不能声明未来观察")
    return "apply"
