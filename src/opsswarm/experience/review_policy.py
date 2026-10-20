"""与数据库及 HTTP 无关的审核版本、幂等与终态规则。"""

from typing import Literal

from opsswarm.experience.contracts import ExperienceError, canonical_hash
from opsswarm.experience.records import CandidateView
from opsswarm.experience.review_contracts import ReviewCommand, ReviewView


def evaluate_review(
    candidate: CandidateView,
    command: ReviewCommand,
    *,
    subject_id: str,
    existing: ReviewView | None,
) -> Literal["apply", "replay"]:
    same_key = (
        existing is not None
        and existing.reviewed_by_id == subject_id
        and existing.idempotency_key == command.idempotency_key
    )
    if same_key and existing is not None and existing.command_hash != canonical_hash(command):
        raise ExperienceError("experience_review_idempotency_conflict", "审核幂等键已绑定不同命令")
    if candidate.version != command.expected_candidate_version:
        raise ExperienceError("experience_candidate_version_conflict", "候选版本与审核请求不一致")
    if candidate.content_hash != command.expected_candidate_content_hash:
        raise ExperienceError("experience_candidate_hash_conflict", "候选内容散列与审核请求不一致")
    if same_key:
        return "replay"
    if existing is not None:
        raise ExperienceError("experience_candidate_already_reviewed", "候选已有不可变审核决定")
    if command.expected_review_version != 0:
        raise ExperienceError("experience_review_version_conflict", "预期审核版本与待审状态不一致")
    return "apply"
