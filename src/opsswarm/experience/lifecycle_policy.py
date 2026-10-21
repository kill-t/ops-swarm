"""停用版本与完整命令重放的确定性规则。"""

from typing import Literal

from opsswarm.experience.contracts import ExperienceError, canonical_hash
from opsswarm.experience.lifecycle_contracts import DisableCommand, DisablementView
from opsswarm.experience.review_contracts import EntryView


def evaluate_disable(
    entry: EntryView,
    command: DisableCommand,
    *,
    subject_id: str,
    existing: DisablementView | None,
    replaced: bool = False,
) -> Literal["apply", "replay"]:
    if replaced:
        raise ExperienceError("experience_entry_already_replaced", "已替代经验不能再停用")
    same_key = (
        existing is not None
        and existing.disabled_by_id == subject_id
        and existing.idempotency_key == command.idempotency_key
    )
    if same_key and existing is not None and existing.command_hash != canonical_hash(command):
        raise ExperienceError("experience_disable_idempotency_conflict", "停用幂等键已绑定不同命令")
    if entry.version != command.expected_entry_version:
        raise ExperienceError("experience_entry_version_conflict", "经验正文版本与停用请求不一致")
    if entry.content_hash != command.expected_entry_content_hash:
        raise ExperienceError("experience_entry_hash_conflict", "经验正文散列与停用请求不一致")
    if same_key:
        return "replay"
    if existing is not None:
        raise ExperienceError("experience_entry_already_disabled", "经验已有不可变停用事实")
    if command.expected_state_version != 1:
        raise ExperienceError("experience_state_version_conflict", "预期有效性版本与当前状态不一致")
    return "apply"
