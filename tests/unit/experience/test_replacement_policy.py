"""真实导出来源上的经验替代、历史重放和互斥终态纯规则验收。"""

from datetime import timedelta
from typing import Any
from uuid import uuid5

import pytest
from pydantic import ValidationError

from opsswarm.experience.artifact_validation import candidate_id_for_job
from opsswarm.experience.contracts import ExperienceError, ExperienceSource, canonical_hash
from opsswarm.experience.lifecycle_contracts import DisableCommand, DisablementView
from opsswarm.experience.lifecycle_policy import evaluate_disable
from opsswarm.experience.lifecycle_validation import disablement_id_for_entry
from opsswarm.experience.projection import project_source
from opsswarm.experience.replacement_contracts import ReplaceCommand, ReplacementView
from opsswarm.experience.replacement_policy import (
    replacement_action,
    validate_replacement,
    validate_replacement_pair,
)
from opsswarm.experience.replacement_validation import replacement_id_for_entry
from opsswarm.experience.review_contracts import ConfirmedExperienceContent, EntryView
from opsswarm.experience.review_validation import entry_id_for_candidate, review_id_for_candidate
from opsswarm.experience.state_reader import project_entry_state
from opsswarm.investigation.models import InvestigationDataSourceMode
from tests.unit.experience.test_lifecycle_policy import source as exported_source_fixture

source = exported_source_fixture


@pytest.fixture
def entries(source: ExperienceSource) -> tuple[EntryView, EntryView]:
    """从已有导出材料确认两份独立工件，不虚构来源事实或递增正文版本。"""
    draft = project_source(source)
    content = ConfirmedExperienceContent(candidate_snapshot=draft)
    result = []
    for ordinal in (1, 2):
        job_id = uuid5(source.report_id, f"unit-replacement-job-{ordinal}")
        candidate_id = candidate_id_for_job(job_id)
        result.append(
            EntryView(
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
                created_at=source.report.created_at + timedelta(seconds=ordinal),
            )
        )
    return result[0], result[1]


def _command(entry: EntryView, target: EntryView, **overrides: Any) -> ReplaceCommand:
    return ReplaceCommand.model_validate(
        {
            "idempotency_key": "replace-unit-001",
            "expected_entry_version": entry.version,
            "expected_entry_content_hash": entry.content_hash,
            "expected_state_version": 1,
            "target_entry_id": target.id,
            "expected_target_version": target.version,
            "expected_target_content_hash": target.content_hash,
            "expected_target_state_version": 1,
            "reason": "采用较晚确认的同范围历史参考，保留旧正文与授权来源",
            **overrides,
        }
    )


def _replacement(entry: EntryView, target: EntryView, command: ReplaceCommand) -> ReplacementView:
    return ReplacementView(
        id=replacement_id_for_entry(entry.id),
        entry_id=entry.id,
        entry_version=entry.version,
        entry_content_hash=entry.content_hash,
        source_hash=entry.source_hash,
        target_entry_id=target.id,
        target_entry_version=target.version,
        target_content_hash=target.content_hash,
        target_source_hash=target.source_hash,
        state_version=2,
        policy_version="experience-replacement-v1",
        replaced_by_id="engineer-1",
        authorization_id=uuid5(entry.id, "unit-replacement-admission"),
        idempotency_key=command.idempotency_key,
        command_hash=canonical_hash(command),
        reason=command.reason,
        replaced_at=target.created_at + timedelta(seconds=1),
    )


def _disable_command(entry: EntryView) -> DisableCommand:
    return DisableCommand(
        idempotency_key="disable-replacement-unit",
        expected_entry_version=entry.version,
        expected_entry_content_hash=entry.content_hash,
        expected_state_version=1,
        reason="停止新检索使用，保留历史确认正文",
    )


def _disablement(entry: EntryView) -> DisablementView:
    command = _disable_command(entry)
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


def test_two_precise_active_artifacts_allow_replacement_without_rewriting_content(
    entries: tuple[EntryView, EntryView], source: ExperienceSource
) -> None:
    entry, target = entries
    before = (entry.model_dump_json(), target.model_dump_json())
    assert entry.id != target.id and target.created_at > entry.created_at
    assert entry.source_hash == target.source_hash == canonical_hash(source)
    assert entry.content_hash == canonical_hash(entry.content)
    assert target.content_hash == canonical_hash(target.content)
    command = _command(entry, target)

    assert replacement_action(command, subject_id="engineer-1", existing=None) == "apply"
    validate_replacement(
        entry, target, command, entry_active=True, target_active=True, target_has_predecessor=False
    )
    state = project_entry_state(entry, None, _replacement(entry, target, command))

    assert state.status == "replaced" and state.state_version == 2
    assert state.entry_version == entry.version == target.version == 1
    assert state.replacement is not None and state.replacement.target_entry_id == target.id
    assert state.disablement is None
    assert (entry.model_dump_json(), target.model_dump_json()) == before


def test_successful_command_replays_even_after_its_target_is_disabled(
    entries: tuple[EntryView, EntryView],
) -> None:
    entry, target = entries
    command = _command(entry, target)
    replacement = _replacement(entry, target, command)
    target_state = project_entry_state(target, _disablement(target))

    assert target_state.status == "disabled"
    assert replacement_action(command, subject_id="engineer-1", existing=replacement) == "replay"
    assert project_entry_state(entry, None, replacement).replacement == replacement


@pytest.mark.parametrize(
    "overrides",
    [
        {"reason": "修改已接受替代的原因"},
        {"target_entry_id": "00000000-0000-0000-0000-000000000108"},
        {"expected_entry_version": 2},
        {"expected_entry_content_hash": "0" * 64},
        {"expected_state_version": 2},
        {"expected_target_version": 2},
        {"expected_target_content_hash": "0" * 64},
        {"expected_target_state_version": 2},
    ],
)
def test_same_key_binds_the_entire_original_command(
    entries: tuple[EntryView, EntryView], overrides: dict[str, Any]
) -> None:
    entry, target = entries
    original = _command(entry, target)
    with pytest.raises(ExperienceError) as error:
        replacement_action(
            _command(entry, target, **overrides),
            subject_id="engineer-1",
            existing=_replacement(entry, target, original),
        )

    assert error.value.code == "experience_replace_idempotency_conflict"


@pytest.mark.parametrize(
    ("subject_id", "key"),
    [("engineer-2", "replace-unit-001"), ("engineer-1", "another-replacement-key")],
)
def test_another_subject_or_command_cannot_claim_a_terminal_replacement(
    entries: tuple[EntryView, EntryView], subject_id: str, key: str
) -> None:
    entry, target = entries
    original = _command(entry, target)
    with pytest.raises(ExperienceError) as error:
        replacement_action(
            _command(entry, target, idempotency_key=key),
            subject_id=subject_id,
            existing=_replacement(entry, target, original),
        )

    assert error.value.code == "experience_entry_already_replaced"


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"expected_entry_version": 2}, "experience_entry_version_conflict"),
        ({"expected_entry_content_hash": "0" * 64}, "experience_entry_hash_conflict"),
        ({"expected_state_version": 2}, "experience_state_version_conflict"),
        ({"expected_target_version": 2}, "experience_target_version_conflict"),
        ({"expected_target_content_hash": "0" * 64}, "experience_target_hash_conflict"),
        ({"expected_target_state_version": 2}, "experience_target_state_conflict"),
    ],
)
def test_first_replacement_requires_both_exact_content_and_state_versions(
    entries: tuple[EntryView, EntryView], overrides: dict[str, Any], code: str
) -> None:
    entry, target = entries
    with pytest.raises(ExperienceError) as error:
        validate_replacement(
            entry,
            target,
            _command(entry, target, **overrides),
            entry_active=True,
            target_active=True,
            target_has_predecessor=False,
        )

    assert error.value.code == code


@pytest.mark.parametrize(
    ("entry_active", "target_active", "code"),
    [
        (False, True, "experience_state_version_conflict"),
        (True, False, "experience_target_state_conflict"),
    ],
)
def test_terminal_source_or_target_cannot_be_used_for_a_new_replacement(
    entries: tuple[EntryView, EntryView], entry_active: bool, target_active: bool, code: str
) -> None:
    entry, target = entries
    with pytest.raises(ExperienceError) as error:
        validate_replacement(
            entry,
            target,
            _command(entry, target),
            entry_active=entry_active,
            target_active=target_active,
            target_has_predecessor=False,
        )

    assert error.value.code == code


def test_target_cannot_merge_two_predecessors(entries: tuple[EntryView, EntryView]) -> None:
    entry, target = entries
    with pytest.raises(ExperienceError) as error:
        validate_replacement(
            entry,
            target,
            _command(entry, target),
            entry_active=True,
            target_active=True,
            target_has_predecessor=True,
        )

    assert error.value.code == "experience_target_predecessor_conflict"


def test_entry_cannot_replace_itself(entries: tuple[EntryView, EntryView]) -> None:
    entry, _ = entries
    with pytest.raises(ExperienceError) as error:
        validate_replacement_pair(entry, entry)

    assert error.value.code == "experience_replace_self_conflict"


@pytest.mark.parametrize("offset", [timedelta(0), timedelta(microseconds=-1)])
def test_equal_or_earlier_confirmation_cannot_create_a_successor(
    entries: tuple[EntryView, EntryView], offset: timedelta
) -> None:
    entry, target = entries
    target = target.model_copy(update={"created_at": entry.created_at + offset})
    with pytest.raises(ExperienceError) as error:
        validate_replacement_pair(entry, target)

    assert error.value.code == "experience_replace_order_conflict"


def test_strictly_later_confirmation_has_no_artificial_minimum_delay(
    entries: tuple[EntryView, EntryView],
) -> None:
    entry, target = entries
    target = target.model_copy(update={"created_at": entry.created_at + timedelta(microseconds=1)})

    validate_replacement_pair(entry, target)


@pytest.mark.parametrize("scope_field", ["service_name", "data_source_mode"])
def test_replacement_cannot_cross_service_or_evidence_source_scope(
    entries: tuple[EntryView, EntryView], scope_field: str
) -> None:
    entry, target = entries
    draft_data = target.content.candidate_snapshot.model_dump(mode="python")
    other_mode = (
        InvestigationDataSourceMode.LIVE
        if draft_data["data_source_mode"] is InvestigationDataSourceMode.FROZEN
        else InvestigationDataSourceMode.FROZEN
    )
    draft_data[scope_field] = "other-service" if scope_field == "service_name" else other_mode
    content = ConfirmedExperienceContent.model_validate({"candidate_snapshot": draft_data})
    target = target.model_copy(
        update={
            "content": content,
            "content_hash": canonical_hash(content),
            "candidate_content_hash": canonical_hash(content.candidate_snapshot),
        }
    )
    assert target.content_hash == canonical_hash(target.content)

    with pytest.raises(ExperienceError) as error:
        validate_replacement_pair(entry, target)

    assert error.value.code == "experience_replace_scope_conflict"


def test_confirmed_contract_cannot_introduce_another_environment(
    entries: tuple[EntryView, EntryView],
) -> None:
    _, target = entries
    data = target.content.model_dump(mode="python")
    data["candidate_snapshot"]["environment"] = "production"

    with pytest.raises(ValidationError):
        ConfirmedExperienceContent.model_validate(data)


@pytest.mark.parametrize(
    "overrides",
    [
        {"idempotency_key": "short"},
        {"idempotency_key": "x" * 101},
        {"idempotency_key": "replace key with spaces"},
        {"reason": " "},
        {"reason": "x" * 1001},
        {"expected_entry_version": 0},
        {"expected_target_version": 2**31},
        {"expected_state_version": 0},
        {"expected_target_state_version": 2**31},
        {"expected_target_content_hash": "A" * 64},
        {"target_entry_id": "not-an-entry-id"},
        {"actor_id": "another-engineer"},
        {"policy_version": "caller-selected-policy"},
        {"content": {"historical_summary": "调用方不得借替代改写确认正文"}},
    ],
)
def test_replacement_command_is_bounded_and_cannot_supply_identity_or_content(
    entries: tuple[EntryView, EntryView], overrides: dict[str, Any]
) -> None:
    with pytest.raises(ValidationError):
        _command(*entries, **overrides)


def test_replacement_command_accepts_exact_limits_and_normalizes_reason(
    entries: tuple[EntryView, EntryView],
) -> None:
    command = _command(
        *entries,
        idempotency_key="a" * 100,
        reason="  " + "x" * 1000 + "  ",
        expected_entry_version=2**31 - 1,
        expected_target_version=2**31 - 1,
        expected_state_version=2**31 - 1,
        expected_target_state_version=2**31 - 1,
    )

    assert len(command.idempotency_key) == 100
    assert command.reason == "x" * 1000


def test_replaced_entry_cannot_be_disabled(entries: tuple[EntryView, EntryView]) -> None:
    entry, _ = entries
    with pytest.raises(ExperienceError) as error:
        evaluate_disable(
            entry, _disable_command(entry), subject_id="engineer-1", existing=None, replaced=True
        )

    assert error.value.code == "experience_entry_already_replaced"


def test_state_projection_fails_closed_for_both_terminal_facts(
    entries: tuple[EntryView, EntryView],
) -> None:
    entry, target = entries
    with pytest.raises(ExperienceError) as error:
        project_entry_state(
            entry, _disablement(entry), _replacement(entry, target, _command(entry, target))
        )

    assert error.value.code == "experience_state_integrity_conflict"
