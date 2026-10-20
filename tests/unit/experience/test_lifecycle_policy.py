"""真实导出来源上的停用纯规则、有限输入与检索摘要单元验收。"""

from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid5

import pytest
from pydantic import ValidationError

from opsswarm.experience.artifact_validation import candidate_id_for_job
from opsswarm.experience.contracts import ExperienceError, ExperienceSource, canonical_hash
from opsswarm.experience.lifecycle_contracts import (
    DisableCommand,
    DisablementView,
    ExperienceSearchQuery,
    ExperienceSearchResult,
    SearchScope,
)
from opsswarm.experience.lifecycle_policy import evaluate_disable
from opsswarm.experience.lifecycle_validation import disablement_id_for_entry
from opsswarm.experience.projection import project_source
from opsswarm.experience.review_contracts import ConfirmedExperienceContent, EntryView
from opsswarm.experience.review_validation import entry_id_for_candidate, review_id_for_candidate
from opsswarm.experience.search_policy import summarize_entry
from opsswarm.investigation.models import EvidenceRelation, InvestigationDataSourceMode

PENDING_SUFFIX = "这些内容仅复述历史记录，尚未经过经验审核。"


@pytest.fixture
def source() -> ExperienceSource:
    """仅消费已有业务链导出材料，不新造数据库来源。"""
    path = Path(__file__).resolve().parents[2] / "fixtures/experience-source-v1.json"
    return ExperienceSource.model_validate_json(path.read_text(encoding="utf-8"))


def _entry_from_source(source: ExperienceSource) -> EntryView:
    draft = project_source(source)
    job_id = uuid5(source.report_id, "unit-lifecycle-job")
    candidate_id = candidate_id_for_job(job_id)
    content = ConfirmedExperienceContent(candidate_snapshot=draft)
    return EntryView(
        id=entry_id_for_candidate(candidate_id),
        review_id=review_id_for_candidate(candidate_id),
        candidate_id=candidate_id,
        candidate_version=1,
        candidate_content_hash=canonical_hash(draft),
        source_hash=canonical_hash(source),
        version=1,
        status="confirmed",
        content=content,
        content_hash=canonical_hash(content),
        created_at=source.report.created_at + timedelta(seconds=1),
    )


@pytest.fixture
def entry(source: ExperienceSource) -> EntryView:
    return _entry_from_source(source)


def _command(entry: EntryView, **overrides: Any) -> DisableCommand:
    return DisableCommand.model_validate(
        {
            "idempotency_key": "disable-unit-001",
            "expected_entry_version": entry.version,
            "expected_entry_content_hash": entry.content_hash,
            "expected_state_version": 1,
            "reason": "历史经验不再适用，停止新检索使用",
            **overrides,
        }
    )


def _disabled(entry: EntryView, command: DisableCommand) -> DisablementView:
    return DisablementView(
        id=disablement_id_for_entry(entry.id),
        entry_id=entry.id,
        entry_version=entry.version,
        entry_content_hash=entry.content_hash,
        source_hash=entry.source_hash,
        state_version=2,
        policy_version="experience-lifecycle-v1",
        disabled_by_id="engineer-1",
        authorization_id=uuid5(entry.id, "unit-disable-admission"),
        idempotency_key=command.idempotency_key,
        command_hash=canonical_hash(command),
        reason=command.reason,
        disabled_at=entry.created_at + timedelta(seconds=1),
    )


def _scope(entry: EntryView, source_incident_id: UUID, **overrides: Any) -> SearchScope:
    draft = entry.content.candidate_snapshot
    return SearchScope.model_validate(
        {
            "run_id": uuid5(entry.id, "target-run"),
            "incident_id": uuid5(source_incident_id, "target-incident"),
            "service_name": draft.service_name,
            "data_source_mode": draft.data_source_mode,
            **overrides,
        }
    )


def test_active_entry_accepts_first_exact_disablement(entry: EntryView) -> None:
    assert (
        evaluate_disable(entry, _command(entry), subject_id="engineer-1", existing=None) == "apply"
    )


def test_disabled_state_replays_original_complete_command(entry: EntryView) -> None:
    command = _command(entry)
    disabled = _disabled(entry, command)

    assert disabled.state_version == 2
    assert evaluate_disable(entry, command, subject_id="engineer-1", existing=disabled) == "replay"


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"expected_entry_version": 2}, "experience_entry_version_conflict"),
        ({"expected_entry_content_hash": "0" * 64}, "experience_entry_hash_conflict"),
        ({"expected_state_version": 2}, "experience_state_version_conflict"),
    ],
)
def test_first_disablement_requires_exact_versions_and_hash(
    entry: EntryView, overrides: dict[str, Any], code: str
) -> None:
    with pytest.raises(ExperienceError) as error:
        evaluate_disable(
            entry, _command(entry, **overrides), subject_id="engineer-1", existing=None
        )

    assert error.value.code == code


@pytest.mark.parametrize(
    "overrides",
    [
        {"reason": "换一个原因"},
        {"expected_entry_version": 2},
        {"expected_entry_content_hash": "0" * 64},
        {"expected_state_version": 2},
    ],
)
def test_same_key_cannot_change_any_part_of_the_original_command(
    entry: EntryView, overrides: dict[str, Any]
) -> None:
    original = _command(entry)
    with pytest.raises(ExperienceError) as error:
        evaluate_disable(
            entry,
            _command(entry, **overrides),
            subject_id="engineer-1",
            existing=_disabled(entry, original),
        )

    assert error.value.code == "experience_disable_idempotency_conflict"


@pytest.mark.parametrize(
    ("subject_id", "key"),
    [("engineer-2", "disable-unit-001"), ("engineer-1", "another-disable-key")],
)
def test_disablement_terminal_state_cannot_be_claimed_by_another_request(
    entry: EntryView, subject_id: str, key: str
) -> None:
    original = _command(entry)
    with pytest.raises(ExperienceError) as error:
        evaluate_disable(
            entry,
            _command(entry, idempotency_key=key),
            subject_id=subject_id,
            existing=_disabled(entry, original),
        )

    assert error.value.code == "experience_entry_already_disabled"


@pytest.mark.parametrize(
    "overrides",
    [
        {"idempotency_key": "short"},
        {"idempotency_key": "a" * 101},
        {"idempotency_key": "中文停用请求001"},
        {"idempotency_key": "disable key has spaces"},
        {"reason": " "},
        {"reason": "x" * 1001},
        {"expected_entry_version": 0},
        {"expected_entry_version": 2**31},
        {"expected_state_version": 0},
        {"expected_entry_content_hash": "A" * 64},
        {"actor_id": "another-engineer"},
        {"policy_version": "caller-policy"},
    ],
)
def test_disable_input_rejects_unsafe_or_unbounded_values(
    entry: EntryView, overrides: dict[str, Any]
) -> None:
    with pytest.raises(ValidationError):
        _command(entry, **overrides)


def test_disable_input_accepts_exact_limits_and_normalizes_reason(entry: EntryView) -> None:
    command = _command(entry, idempotency_key="a" * 100, reason="  " + "x" * 1000 + "  ")

    assert len(command.idempotency_key) == 100
    assert command.reason == "x" * 1000


@pytest.mark.parametrize(
    "overrides", [{"service_name": "other"}, {"environment": "production"}, {"limit": 4}]
)
def test_search_input_cannot_override_system_scope_or_raise_limit(
    overrides: dict[str, Any],
) -> None:
    with pytest.raises(ValidationError):
        ExperienceSearchQuery.model_validate(overrides)


@pytest.mark.parametrize("mismatch", ["service_name", "data_source_mode"])
def test_summary_rejects_out_of_scope_entry(
    entry: EntryView, source: ExperienceSource, mismatch: str
) -> None:
    different_mode = (
        InvestigationDataSourceMode.LIVE
        if source.data_source_mode is InvestigationDataSourceMode.FROZEN
        else InvestigationDataSourceMode.FROZEN
    )
    scope = _scope(
        entry,
        source.incident_id,
        **{mismatch: "other-service" if mismatch == "service_name" else different_mode},
    )
    with pytest.raises(ExperienceError) as error:
        summarize_entry(entry, source.incident_id, scope)

    assert error.value.code == "experience_search_scope_conflict"


def test_summary_rejects_experience_from_the_target_incident(
    entry: EntryView, source: ExperienceSource
) -> None:
    scope = _scope(entry, source.incident_id, incident_id=source.incident_id)

    with pytest.raises(ExperienceError) as error:
        summarize_entry(entry, source.incident_id, scope)

    assert error.value.code == "experience_search_scope_conflict"


@pytest.mark.parametrize("relation", list(EvidenceRelation))
def test_summary_preserves_counterevidence_signal(
    source: ExperienceSource, relation: EvidenceRelation
) -> None:
    data = source.model_dump(mode="json")
    for assessment in data["hypothesis"]["evidence_assessments"]:
        assessment["relation"] = relation.value
    varied_source = ExperienceSource.model_validate(data)
    entry = _entry_from_source(varied_source)

    item = summarize_entry(entry, source.incident_id, _scope(entry, source.incident_id))

    assert item.has_refuting_evidence is (relation is EvidenceRelation.REFUTES)
    assert item.evidence_count == len(entry.content.candidate_snapshot.evidence_refs)


def test_summary_removes_only_pending_suffix_without_rewriting_confirmed_artifact(
    entry: EntryView, source: ExperienceSource
) -> None:
    before = entry.model_dump_json()
    summary = entry.content.candidate_snapshot.historical_summary
    assert summary.endswith(PENDING_SUFFIX)

    item = summarize_entry(entry, source.incident_id, _scope(entry, source.incident_id))

    assert item.historical_summary == summary.removesuffix(PENDING_SUFFIX)
    assert "尚未经过经验审核" not in item.historical_summary
    assert item.confirmation_notice == entry.content.confirmation_notice
    assert item.content_hash == entry.content_hash
    assert item.source_hash == entry.source_hash
    assert item.state_version == 1
    assert entry.model_dump_json() == before
    assert entry.status == "confirmed" and entry.version == 1


def test_search_contract_never_returns_more_than_three_items(
    entry: EntryView, source: ExperienceSource
) -> None:
    scope = _scope(entry, source.incident_id)
    item = summarize_entry(entry, source.incident_id, scope)

    with pytest.raises(ValidationError, match="at most 3"):
        ExperienceSearchResult(
            scope=scope,
            preferred_fault_tag=None,
            retrieved_at=entry.created_at,
            items=(item, item, item, item),
        )
