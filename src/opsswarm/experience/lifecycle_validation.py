"""停用事实的精确身份、完整命令及可信授权校验。"""

from uuid import NAMESPACE_URL, UUID, uuid5

from opsswarm.experience.contracts import ExperienceError, canonical_hash
from opsswarm.experience.lifecycle_contracts import DisableCommand, DisablementView
from opsswarm.experience.lifecycle_tables import ExperienceDisablementRow
from opsswarm.experience.review_contracts import EntryView
from opsswarm.identity.tables import OperatorAuthorizationRow


def disablement_id_for_entry(entry_id: UUID) -> UUID:
    return uuid5(NAMESPACE_URL, f"opsswarm:experience-disabled:{entry_id}:2")


def validated_disablement(
    row: ExperienceDisablementRow,
    entry: EntryView,
    admission: OperatorAuthorizationRow | None,
) -> DisablementView:
    try:
        result = DisablementView.model_validate(row, from_attributes=True)
        command = DisableCommand(
            idempotency_key=result.idempotency_key,
            expected_entry_version=entry.version,
            expected_entry_content_hash=entry.content_hash,
            expected_state_version=1,
            reason=result.reason,
        )
        if (
            result.id != disablement_id_for_entry(entry.id)
            or result.entry_id != entry.id
            or result.entry_version != entry.version
            or result.entry_content_hash != entry.content_hash
            or result.source_hash != entry.source_hash
            or result.command_hash != canonical_hash(command)
            or result.disabled_at < entry.created_at
            or admission is None
            or admission.subject_id != result.disabled_by_id
            or admission.role != "engineer"
            or admission.operation != "disable_experience_entry"
            or admission.method != "POST"
            or admission.resources != {"entry_id": str(entry.id)}
            or "manage_report" not in admission.permissions
            or admission.authorized_at > result.disabled_at
        ):
            raise ValueError
        return result
    except ValueError:
        raise ExperienceError(
            "experience_disablement_integrity_conflict", "停用事实与确认版本或授权不一致"
        ) from None
