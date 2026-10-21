"""替代的完整命令重放及双侧有效性、范围与时间顺序规则。"""

from typing import Literal

from opsswarm.experience.contracts import ExperienceError, canonical_hash
from opsswarm.experience.replacement_contracts import ReplaceCommand, ReplacementView
from opsswarm.experience.review_contracts import EntryView


def replacement_action(
    command: ReplaceCommand, *, subject_id: str, existing: ReplacementView | None
) -> Literal["apply", "replay"]:
    if existing is None:
        return "apply"
    if (
        existing.replaced_by_id == subject_id
        and existing.idempotency_key == command.idempotency_key
    ):
        if existing.command_hash != canonical_hash(command):
            raise ExperienceError("experience_replace_idempotency_conflict", "替代键已绑定不同命令")
        return "replay"
    raise ExperienceError("experience_entry_already_replaced", "经验已有不可变替代事实")


def validate_replacement_pair(entry: EntryView, target: EntryView) -> None:
    if entry.id == target.id:
        raise ExperienceError("experience_replace_self_conflict", "经验不能替代自身")
    old = entry.content.candidate_snapshot
    new = target.content.candidate_snapshot
    if (old.service_name, old.environment, old.data_source_mode) != (
        new.service_name,
        new.environment,
        new.data_source_mode,
    ):
        raise ExperienceError("experience_replace_scope_conflict", "新旧经验适用范围不一致")
    if target.created_at <= entry.created_at:
        raise ExperienceError("experience_replace_order_conflict", "后继必须具有严格较晚的确认时间")


def validate_replacement(
    entry: EntryView,
    target: EntryView,
    command: ReplaceCommand,
    *,
    entry_active: bool,
    target_active: bool,
    target_has_predecessor: bool,
) -> None:
    if entry.version != command.expected_entry_version:
        raise ExperienceError("experience_entry_version_conflict", "原经验正文版本不一致")
    if entry.content_hash != command.expected_entry_content_hash:
        raise ExperienceError("experience_entry_hash_conflict", "原经验正文散列不一致")
    if target.version != command.expected_target_version:
        raise ExperienceError("experience_target_version_conflict", "目标经验正文版本不一致")
    if target.content_hash != command.expected_target_content_hash:
        raise ExperienceError("experience_target_hash_conflict", "目标经验正文散列不一致")
    if not entry_active or command.expected_state_version != 1:
        raise ExperienceError("experience_state_version_conflict", "原经验必须为有效版本一")
    if not target_active or command.expected_target_state_version != 1:
        raise ExperienceError("experience_target_state_conflict", "目标经验必须为有效版本一")
    if target_has_predecessor:
        raise ExperienceError("experience_target_predecessor_conflict", "目标经验已被其他前驱使用")
    validate_replacement_pair(entry, target)
