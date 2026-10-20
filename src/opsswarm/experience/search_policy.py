"""已确认历史材料的有界摘要投影，不增加当前事故判断。"""

from uuid import UUID

from opsswarm.experience.contracts import ExperienceError
from opsswarm.experience.lifecycle_contracts import ExperienceSearchItem, SearchScope
from opsswarm.experience.review_contracts import EntryView
from opsswarm.investigation.models import EvidenceRelation


def summarize_entry(
    entry: EntryView, source_incident_id: UUID, scope: SearchScope
) -> ExperienceSearchItem:
    draft = entry.content.candidate_snapshot
    if (
        draft.service_name != scope.service_name
        or draft.environment != scope.environment
        or draft.data_source_mode != scope.data_source_mode
        or source_incident_id == scope.incident_id
    ):
        raise ExperienceError("experience_search_scope_conflict", "经验与系统确定的检索范围不一致")
    return ExperienceSearchItem(
        entry_id=entry.id,
        entry_version=entry.version,
        content_hash=entry.content_hash,
        source_hash=entry.source_hash,
        source_incident_id=source_incident_id,
        historical_summary=draft.historical_summary.removesuffix(
            "这些内容仅复述历史记录，尚未经过经验审核。"
        ),
        confirmation_notice=entry.content.confirmation_notice,
        fault_tags=draft.fault_tags,
        has_refuting_evidence=any(
            EvidenceRelation.REFUTES in item.relations for item in draft.evidence_refs
        ),
        evidence_count=len(draft.evidence_refs),
        confirmed_at=entry.created_at,
    )
