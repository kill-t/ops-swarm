"""审核决定与确认工件的确定性身份和同源校验。"""

from uuid import NAMESPACE_URL, UUID, uuid5

from opsswarm.experience.contracts import ExperienceError, canonical_hash
from opsswarm.experience.records import CandidateView
from opsswarm.experience.review_contracts import (
    ConfirmedExperienceContent,
    EntryView,
    ReviewCommand,
    ReviewDecision,
    ReviewView,
)
from opsswarm.experience.review_tables import ExperienceEntryRow, ExperienceReviewRow
from opsswarm.identity.tables import OperatorAuthorizationRow


def review_id_for_candidate(candidate_id: UUID) -> UUID:
    return uuid5(NAMESPACE_URL, f"opsswarm:experience-review:{candidate_id}:1")


def entry_id_for_candidate(candidate_id: UUID) -> UUID:
    return uuid5(NAMESPACE_URL, f"opsswarm:experience-entry:{candidate_id}:1")


def validated_review(
    row: ExperienceReviewRow,
    candidate: CandidateView,
    admission: OperatorAuthorizationRow | None,
) -> ReviewView:
    try:
        result = ReviewView.model_validate(row, from_attributes=True)
        command = ReviewCommand(
            idempotency_key=result.idempotency_key,
            expected_candidate_version=candidate.version,
            expected_candidate_content_hash=candidate.content_hash,
            expected_review_version=0,
            decision=result.decision,
            reason=result.reason,
        )
        if (
            result.id != review_id_for_candidate(candidate.id)
            or result.candidate_id != candidate.id
            or result.candidate_version != candidate.version
            or result.candidate_content_hash != candidate.content_hash
            or result.source_hash != candidate.source_hash
            or result.command_hash != canonical_hash(command)
            or result.reviewed_at < candidate.created_at
            or admission is None
            or admission.subject_id != result.reviewed_by_id
            or admission.role != "engineer"
            or admission.operation != "review_experience_candidate"
            or admission.resources != {"candidate_id": str(candidate.id)}
            or admission.method != "POST"
            or "manage_report" not in admission.permissions
            or admission.authorized_at > result.reviewed_at
        ):
            raise ValueError
        return result
    except ValueError:
        raise ExperienceError(
            "experience_review_integrity_conflict", "审核决定与候选或授权不一致"
        ) from None


def validated_entry(
    row: ExperienceEntryRow,
    candidate: CandidateView,
    review: ReviewView,
) -> EntryView:
    try:
        result = EntryView.model_validate(row, from_attributes=True)
        expected = ConfirmedExperienceContent(candidate_snapshot=candidate.content)
        if (
            review.decision is not ReviewDecision.CONFIRM
            or row.review_decision != "confirm"
            or result.id != entry_id_for_candidate(candidate.id)
            or result.review_id != review.id
            or result.candidate_id != candidate.id
            or result.candidate_version != candidate.version
            or result.candidate_content_hash != candidate.content_hash
            or result.source_hash != candidate.source_hash
            or result.content != expected
            or result.content_hash != canonical_hash(expected)
            or result.created_at != review.reviewed_at
        ):
            raise ValueError
        return result
    except ValueError:
        raise ExperienceError(
            "experience_entry_integrity_conflict", "确认经验与决定或来源不一致"
        ) from None
