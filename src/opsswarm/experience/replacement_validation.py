"""替代事实的双侧精确工件、完整命令和首次可信授权校验。"""

from uuid import NAMESPACE_URL, UUID, uuid5

from opsswarm.experience.contracts import ExperienceError, canonical_hash
from opsswarm.experience.replacement_contracts import ReplaceCommand, ReplacementView
from opsswarm.experience.replacement_policy import validate_replacement_pair
from opsswarm.experience.replacement_tables import ExperienceReplacementRow
from opsswarm.experience.review_contracts import EntryView
from opsswarm.identity.tables import OperatorAuthorizationRow


def replacement_id_for_entry(entry_id: UUID) -> UUID:
    return uuid5(NAMESPACE_URL, f"opsswarm:experience-replaced:{entry_id}:2")


def validated_replacement(
    row: ExperienceReplacementRow,
    entry: EntryView,
    target: EntryView,
    admission: OperatorAuthorizationRow | None,
) -> ReplacementView:
    try:
        result = ReplacementView.model_validate(row, from_attributes=True)
        command = ReplaceCommand(
            idempotency_key=result.idempotency_key,
            expected_entry_version=entry.version,
            expected_entry_content_hash=entry.content_hash,
            expected_state_version=1,
            target_entry_id=target.id,
            expected_target_version=target.version,
            expected_target_content_hash=target.content_hash,
            expected_target_state_version=1,
            reason=result.reason,
        )
        validate_replacement_pair(entry, target)
        if (
            result.id != replacement_id_for_entry(entry.id)
            or (
                result.entry_id,
                result.entry_version,
                result.entry_content_hash,
                result.source_hash,
            )
            != (entry.id, entry.version, entry.content_hash, entry.source_hash)
            or (
                result.target_entry_id,
                result.target_entry_version,
                result.target_content_hash,
                result.target_source_hash,
            )
            != (target.id, target.version, target.content_hash, target.source_hash)
            or result.command_hash != canonical_hash(command)
            or result.replaced_at < target.created_at
            or admission is None
            or admission.subject_id != result.replaced_by_id
            or admission.role != "engineer"
            or admission.operation != "replace_experience_entry"
            or admission.method != "POST"
            or admission.resources != {"entry_id": str(entry.id)}
            or "manage_report" not in admission.permissions
            or admission.authorized_at > result.replaced_at
        ):
            raise ValueError
        return result
    except (ValueError, ExperienceError):
        raise ExperienceError(
            "experience_replacement_integrity_conflict", "替代事实与双侧工件或可信授权不一致"
        ) from None
