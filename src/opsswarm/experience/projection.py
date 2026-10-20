"""把已验证来源投影为待审核事实草稿，不归纳新规律。"""

from opsswarm.experience.contracts import (
    ExperienceDraft,
    ExperienceError,
    ExperienceEvidenceReference,
    ExperienceSource,
)


def project_source(source: ExperienceSource) -> ExperienceDraft:
    """保留原判断强度与反证关系，不输出处置指令。"""
    try:
        return _project_source(source)
    except ExperienceError:
        raise
    except ValueError:
        raise ExperienceError("source_conflict", "经验来源或候选投影校验失败") from None


def _project_source(source: ExperienceSource) -> ExperienceDraft:
    source = ExperienceSource.model_validate(source.model_dump(mode="python"))
    hypothesis = source.hypothesis
    verification = source.verification
    relations = {item.evidence_item_id: item.relation for item in hypothesis.evidence_assessments}
    return ExperienceDraft(
        service_name=source.service_name,
        data_source_mode=source.data_source_mode,
        historical_summary=(
            f"历史事故 {source.report.content.incident.reference} 已关闭。"
            f"当时记录的假设为：{hypothesis.statement}；判断状态为 {hypothesis.status.value}，"
            f"置信度为 {hypothesis.confidence}。"
            f"历史执行状态为 {source.execution.status.value}；"
            f"验证 {verification.rule.signal} 的状态为 {verification.status.value}，"
            f"成功样本 {verification.successful_sample_count} 条。"
            "这些内容仅复述历史记录，尚未经过经验审核。"
        ),
        fault_tags=(hypothesis.root_cause.value,),
        preconditions=(
            f"适用服务仅为 {source.service_name}",
            "适用环境仅为隔离本地 Docker Compose 演练环境",
            f"调查材料来源模式为 {source.data_source_mode.value}，不得无说明跨范围使用",
            f"验证样本独立来源为 {verification.verifier_name}",
        ),
        evidence_refs=tuple(
            ExperienceEvidenceReference(
                evidence_item_id=item.id,
                evidence_id=item.observation.evidence_id,
                content_hash=item.content_hash,
                kind=item.observation.kind,
                relations=(relations[item.id],) if item.id in relations else (),
            )
            for item in source.evidence
        ),
        sample_ids=tuple(item.id for item in verification.samples),
        hypothesis_status=hypothesis.status.value,
        verifier_name=verification.verifier_name,
    )
