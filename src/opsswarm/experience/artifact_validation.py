"""生成与审核共用的冻结来源及候选工件完整性校验。"""

from uuid import NAMESPACE_URL, UUID, uuid5

from opsswarm.experience.contracts import (
    GENERATION_POLICY_VERSION,
    ExperienceDraft,
    ExperienceError,
    ExperienceSource,
    canonical_hash,
)
from opsswarm.experience.projection import project_source
from opsswarm.experience.records import CandidateView
from opsswarm.experience.tables import ExperienceCandidateRow, ExperienceJobRow


def candidate_id_for_job(job_id: UUID) -> UUID:
    return uuid5(NAMESPACE_URL, f"opsswarm:experience:{job_id}:1")


def validated_source(row: ExperienceJobRow) -> ExperienceSource:
    try:
        source = ExperienceSource.model_validate(row.source)
        if (
            canonical_hash(source) != row.source_hash
            or source.report_id != row.report_id
            or row.policy_version != GENERATION_POLICY_VERSION
            or row.max_attempts != 3
        ):
            raise ValueError
        return source
    except ValueError:
        raise ExperienceError("source_conflict", "已持久化的经验来源无法校验") from None


def validated_candidate(row: ExperienceCandidateRow, job: ExperienceJobRow) -> CandidateView:
    try:
        draft = ExperienceDraft.model_validate(row.content)
        if (
            row.id != candidate_id_for_job(job.id)
            or row.job_id != job.id
            or row.source_hash != job.source_hash
            or row.content_hash != canonical_hash(draft)
            or draft != project_source(validated_source(job))
        ):
            raise ValueError
        return CandidateView.model_validate(
            {
                "id": row.id,
                "job_id": row.job_id,
                "version": row.version,
                "status": row.status,
                "source_hash": row.source_hash,
                "content": draft,
                "content_hash": row.content_hash,
                "created_at": row.created_at,
            }
        )
    except ValueError:
        raise ExperienceError("candidate_conflict", "已持久化的经验候选无法校验") from None
