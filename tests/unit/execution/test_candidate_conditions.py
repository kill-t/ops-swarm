"""完整条件核验的原文身份、来源约束、状态优先级与连续版本规则。"""

import json
from datetime import UTC, datetime, timedelta, timezone
from hashlib import sha256
from uuid import uuid4

import pytest
from pydantic import ValidationError

from opsswarm.approval.decision.contracts import DecisionContent, DecisionRecord
from opsswarm.execution.conditions.contracts import (
    POLICY_VERSION,
    ConditionCommand,
    ConditionContent,
    ConditionError,
    ConditionInput,
    ConditionItem,
    ConditionRecord,
    ConditionResultItem,
    ConditionState,
    EvidenceReference,
    HumanObservation,
    PreflightReference,
)
from opsswarm.execution.conditions.policy import (
    build_catalog,
    build_content,
    expires_at,
    validate_version,
    verification_availability,
)
from opsswarm.investigation.context.hashing import content_hash
from opsswarm.planning.candidate.contracts import PlanContent, PlanRecord
from opsswarm.planning.models import ActionPlan, ActionPlanStatus
from tests.unit.execution.test_candidate_preflight import step_for

NOW = datetime(2026, 10, 25, 6, tzinfo=UTC)


def plan_for(
    *, preconditions=("核实连接池当前状态", "保留现有连接"), questions=("核实连接池当前状态",)
):
    step, job_id = step_for(), uuid4()
    content = PlanContent(
        candidate_job_id=job_id,
        candidate_result_sha256="a" * 64,
        open_hypothesis_id=uuid4(),
        evidence_ids=("configuration-1",),
        preconditions=preconditions,
        unresolved_questions=questions,
        plan=ActionPlan(
            version=1,
            status=ActionPlanStatus.DRAFT,
            summary="原候选单步骤计划",
            risk_level=step.risk_level,
            requires_human_approval=step.requires_human_approval,
            risk_policy_version="action-risk-v3",
            steps=(step,),
        ),
    )
    return PlanRecord(
        id=uuid4(),
        investigation_run_id=uuid4(),
        candidate_job_id=job_id,
        candidate_result_sha256=content.candidate_result_sha256,
        wait_version=None,
        incident_version=2,
        policy_version="candidate-plan-v1",
        requested_by_id="engineer-1",
        authorization_id=uuid4(),
        idempotency_key="condition-plan-1",
        command_sha256="b" * 64,
        content=content,
        content_sha256=content_hash(content),
        created_at=NOW - timedelta(seconds=30),
    )


def decision_for(plan):
    content = DecisionContent(
        request_id=uuid4(),
        plan_id=plan.id,
        plan_content_sha256=plan.content_sha256,
        candidate_job_id=plan.candidate_job_id,
        source_content_sha256="a" * 64,
        wait_version=plan.wait_version,
        outcome="approved",
        evidence_ids=plan.content.evidence_ids,
        preconditions=plan.content.preconditions,
        unresolved_questions=plan.content.unresolved_questions,
    )
    return DecisionRecord(
        id=uuid4(),
        investigation_run_id=plan.investigation_run_id,
        candidate_job_id=plan.candidate_job_id,
        request_id=content.request_id,
        plan_content_sha256=plan.content_sha256,
        previous_incident_version=3,
        incident_version=4,
        event_cursor=5,
        outcome="approved",
        policy_version="candidate-approval-decision-v1",
        decided_by_id="engineer-1",
        authorization_id=uuid4(),
        idempotency_key="condition-decision-1",
        command_sha256="b" * 64,
        content=content,
        content_sha256=content_hash(content),
        created_at=NOW - timedelta(seconds=10),
    )


def command_for(plan, decision, *, statuses=None, **changes):
    catalog = build_catalog(plan)
    statuses = statuses or ("verified",) * len(catalog)
    return ConditionCommand.model_validate(
        {
            "decision_id": decision.id,
            "decision_content_sha256": decision.content_sha256,
            "expected_incident_version": decision.incident_version,
            "expected_condition_version": 0,
            "previous_content_sha256": None,
            "items": tuple(
                ConditionInput(
                    condition_id=condition.condition_id,
                    status=status,
                    explanation="可信工程师核对后记录该判断",
                    sources=(EvidenceReference(evidence_id="configuration-1"),),
                )
                for condition, status in zip(catalog, statuses, strict=True)
            ),
            "idempotency_key": "conditions-unit-1",
            "reason": "提交完整原条件核验快照",
            **changes,
        }
    )


def record_for(plan, decision, *, version=1, **changes):
    command = command_for(plan, decision)
    content = build_content(decision, plan, command)
    return ConditionRecord.model_validate(
        {
            "id": uuid4(),
            "investigation_run_id": plan.investigation_run_id,
            "candidate_job_id": plan.candidate_job_id,
            "decision_id": decision.id,
            "decision_content_sha256": decision.content_sha256,
            "version": version,
            "previous_id": None if version == 1 else uuid4(),
            "previous_content_sha256": None if version == 1 else "a" * 64,
            "incident_version": decision.incident_version,
            "policy_version": POLICY_VERSION,
            "verified_by_id": "engineer-1",
            "authorization_id": uuid4(),
            "idempotency_key": command.idempotency_key,
            "command_sha256": content_hash(command),
            "content": content,
            "content_sha256": content_hash(content),
            "created_at": NOW,
            "expires_at": NOW + timedelta(seconds=300),
            **changes,
        }
    )


def test_catalog_binds_exact_plan_kind_position_and_original_text():
    plan = plan_for()
    catalog = build_catalog(plan)
    assert catalog == build_catalog(plan)
    assert tuple((item.kind, item.position) for item in catalog) == (
        ("precondition", 1),
        ("precondition", 2),
        ("question", 1),
    )
    assert catalog[0].text == catalog[2].text
    assert catalog[0].condition_id != catalog[2].condition_id
    for item in catalog:
        assert item.text_sha256 == sha256(item.text.encode("utf-8")).hexdigest()
        identity = {
            "plan_id": str(plan.id),
            "plan_content_sha256": plan.content_sha256,
            "kind": item.kind,
            "position": item.position,
            "text_sha256": item.text_sha256,
        }
        expected = sha256(
            json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()
        assert item.condition_id == expected
    rebound = build_catalog(plan.model_copy(update={"id": uuid4()}))
    rehashed = build_catalog(plan.model_copy(update={"content_sha256": "f" * 64}))
    assert {item.condition_id for item in catalog}.isdisjoint(item.condition_id for item in rebound)
    assert {item.condition_id for item in catalog}.isdisjoint(
        item.condition_id for item in rehashed
    )


@pytest.mark.parametrize("position", [0, 11, True, 1.0, "1"])
def test_catalog_position_rejects_outside_range_and_coercion(position):
    with pytest.raises(ValidationError):
        ConditionItem(
            condition_id="a" * 64,
            kind="precondition",
            position=position,
            text="原条件",
            text_sha256="b" * 64,
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"expected_incident_version": 1},
        {"expected_incident_version": True},
        {"expected_incident_version": "4"},
        {"expected_incident_version": 4.0},
        {"expected_condition_version": -1},
        {"expected_condition_version": 8},
        {"expected_condition_version": True},
        {"expected_condition_version": "0"},
        {"expected_condition_version": 0.0},
        {"expected_condition_version": 1},
        {"previous_content_sha256": "a" * 64},
        {"decision_content_sha256": "A" * 64},
        {"reason": " "},
        {"reason": "原" * 501},
        {"idempotency_key": "short"},
        {"idempotency_key": "invalid/key"},
        {"items": ()},
        {"approval_required": False},
        {"execution_eligible": True},
    ],
)
def test_command_rejects_implicit_types_invalid_cas_and_client_authority(changes):
    plan = plan_for()
    with pytest.raises(ValidationError):
        command_for(plan, decision_for(plan), **changes)


def test_previous_hash_and_sources_are_required_even_when_null_or_empty():
    plan = plan_for()
    data = command_for(plan, decision_for(plan)).model_dump()
    del data["previous_content_sha256"]
    with pytest.raises(ValidationError):
        ConditionCommand.model_validate(data)
    with pytest.raises(ValidationError):
        ConditionInput(condition_id="a" * 64, status="unresolved", explanation="尚不清楚")
    assert (
        ConditionInput(
            condition_id="a" * 64, status="unresolved", explanation="尚不清楚", sources=()
        ).sources
        == ()
    )


@pytest.mark.parametrize("status", ["verified", "not_applicable"])
def test_assertions_require_sources_in_input_and_saved_content(status):
    item = build_catalog(plan_for())[0]
    for model, identity in (
        (ConditionInput, {"condition_id": item.condition_id}),
        (ConditionResultItem, {"condition": item}),
    ):
        with pytest.raises(ValidationError):
            model.model_validate(
                {**identity, "status": status, "explanation": "有结论", "sources": ()}
            )


@pytest.mark.parametrize(
    "source",
    [
        {"kind": "arbitrary", "text": "来源"},
        {"kind": "evidence", "evidence_id": "Configuration-1"},
        {"kind": "evidence", "evidence_id": "configuration-1", "text": "伪造工具结果"},
        {"kind": "human_observation", "text": "观测", "observed_at": "2026-10-25T06:00:00"},
        {"kind": "human_observation", "text": " ", "observed_at": NOW},
        {"kind": "human_observation", "text": "观测", "observed_at": True},
        {"kind": "human_observation", "text": "观测", "observed_at": 1792908000},
        {"kind": "human_observation", "text": "观测", "observed_at": 1792908000.1},
        {"kind": "human_observation", "text": "观测", "observed_at": "1792908000"},
        {"kind": "human_observation", "text": "观测", "observed_at": "1792908000.1"},
        {"kind": "human_observation", "text": "观测", "observed_at": "20261025T060000Z"},
        {"kind": "human_observation", "text": "观测", "observed_at": "2026-10-25T24:00:00Z"},
        {"kind": "human_observation", "text": "观测", "observed_at": "2026-10-25T06:00:60Z"},
        {"kind": "human_observation", "text": "观测", "observed_at": "2026-10-25T06:00:00+16:00"},
        {"kind": "human_observation", "text": "观测", "observed_at": "2026-10-25T06:00:00+01:60"},
        {
            "kind": "human_observation",
            "text": "观测",
            "observed_at": "2026-10-25T06:00:00.1234567Z",
        },
        {"kind": "human_observation", "text": "观测", "observed_at": "2026-10-25t06:00:00Z"},
        {"kind": "human_observation", "text": "观测", "observed_at": "2026-10-25 06:00:00Z"},
        {"kind": "human_observation", "text": "观测", "observed_at": "2026-10-25T06:00:00z"},
        {
            "kind": "human_observation",
            "text": "观测",
            "observed_at": "2026-10-25T06:00:00+08:00:30",
        },
        {"kind": "preflight", "preflight_id": "invalid", "content_sha256": "a" * 64},
        {"kind": "preflight", "preflight_id": str(uuid4()), "content_sha256": "A" * 64},
    ],
)
def test_sources_are_closed_discriminated_union(source):
    with pytest.raises(ValidationError):
        ConditionInput.model_validate(
            {
                "condition_id": "a" * 64,
                "status": "verified",
                "explanation": "明确结论",
                "sources": (source,),
            }
        )


@pytest.mark.parametrize(
    "value,expected",
    [
        ("2026-10-25T06:00:00Z", NOW),
        ("2026-10-25T06:00:00+00:00", NOW),
        ("2026-10-25T14:00:00+08:00", NOW),
        ("2026-10-25T06:00:00.1Z", NOW.replace(microsecond=100000)),
        ("2026-10-25T06:00:00.123456Z", NOW.replace(microsecond=123456)),
    ],
)
def test_observation_accepts_only_explicit_iso8601_bounded_precision(value, expected):
    observation = HumanObservation(text="人工观察", observed_at=value)
    assert observation.observed_at == expected
    assert observation.observed_at.tzinfo is UTC


@pytest.mark.parametrize(
    "value",
    [
        "0001-01-01T00:00:00+15:00",
        "9999-12-31T23:59:59-15:00",
    ],
)
def test_observation_rejects_dates_outside_utc_datetime_range(value):
    with pytest.raises(ValidationError, match="有效日期范围"):
        HumanObservation(text="边界时间的人工观察", observed_at=value)


def test_human_observation_normalizes_utc_and_duplicate_full_sources_are_rejected():
    offset = timezone(timedelta(hours=8))
    source = HumanObservation(text="明确人工观察", observed_at=NOW.astimezone(offset))
    assert source.observed_at.tzinfo is UTC
    assert source.observed_at == NOW
    for sources in (
        (source, source),
        (EvidenceReference(evidence_id="e-1"),) * 2,
        (PreflightReference(preflight_id=uuid4(), content_sha256="a" * 64),) * 2,
        tuple(EvidenceReference(evidence_id=f"e-{index}") for index in range(4)),
    ):
        with pytest.raises(ValidationError):
            ConditionInput(
                condition_id="a" * 64, status="verified", explanation="结论", sources=sources
            )
    distinct = source.model_copy(update={"text": "另一份人工陈述"})
    assert (
        len(
            ConditionInput(
                condition_id="a" * 64,
                status="verified",
                explanation="结论",
                sources=(source, distinct),
            ).sources
        )
        == 2
    )


def test_snapshot_rejects_duplicate_condition_ids_and_more_than_three_preflights():
    plan = plan_for(preconditions=tuple(f"条件{index}" for index in range(4)), questions=())
    decision = decision_for(plan)
    command = command_for(plan, decision)
    with pytest.raises(ValidationError):
        command_for(plan, decision, items=command.items + (command.items[0],))
    items = tuple(
        item.model_copy(
            update={"sources": (PreflightReference(preflight_id=uuid4(), content_sha256="a" * 64),)}
        )
        for item in command.items
    )
    with pytest.raises(ValidationError):
        command_for(plan, decision, items=items)
    assert (
        len(
            command_for(
                plan,
                decision,
                items=items[:3]
                + (items[0].model_copy(update={"condition_id": items[3].condition_id}),),
            ).items
        )
        == 4
    )


def test_canonical_command_enforces_utf8_bytes_and_accepts_bounded_full_snapshot():
    plan = plan_for(
        preconditions=tuple(f"条件{index}" for index in range(10)),
        questions=tuple(f"问题{index}" for index in range(10)),
    )
    decision = decision_for(plan)
    command = command_for(plan, decision)
    assert len(command.items) == 20
    items = tuple(item.model_copy(update={"explanation": "原" * 500}) for item in command.items)
    with pytest.raises(ValidationError, match="24KiB"):
        command_for(plan, decision, items=items)


@pytest.mark.parametrize(
    "statuses,expected",
    [
        (("verified", "verified", "verified"), "ready_for_review"),
        (("verified", "not_applicable", "verified"), "plan_revision_required"),
        (("verified", "not_applicable", "unresolved"), "blocked"),
        (("unresolved", "verified", "verified"), "blocked"),
    ],
)
def test_complete_snapshot_derives_status_without_execution_authority(statuses, expected):
    plan = plan_for()
    decision = decision_for(plan)
    command = command_for(plan, decision, statuses=statuses)
    original = plan.model_dump_json(), decision.model_dump_json(), command.model_dump_json()
    content = build_content(decision, plan, command)
    assert content.status == expected
    assert tuple(item.condition for item in content.items) == build_catalog(plan)
    assert content.approval_required is True and content.execution_eligible is False
    assert original == (
        plan.model_dump_json(),
        decision.model_dump_json(),
        command.model_dump_json(),
    )
    assert content.plan_id == plan.id and content.decision_id == decision.id


@pytest.mark.parametrize("mutation", ["missing", "extra", "reordered", "other_id"])
def test_content_requires_exact_complete_catalog_order(mutation):
    plan = plan_for()
    decision = decision_for(plan)
    command = command_for(plan, decision)
    items = command.items
    if mutation == "missing":
        items = items[:-1]
    elif mutation == "extra":
        items += (items[0].model_copy(update={"condition_id": "a" * 64}),)
    elif mutation == "reordered":
        items = tuple(reversed(items))
    else:
        items = (items[0].model_copy(update={"condition_id": "a" * 64}),) + items[1:]
    with pytest.raises(ConditionError, match="完整覆盖"):
        build_content(decision, plan, command_for(plan, decision, items=items))


@pytest.mark.parametrize(
    "target,changes",
    [
        ("decision", {"outcome": "rejected"}),
        ("decision", {"candidate_job_id": uuid4()}),
        ("decision", {"investigation_run_id": uuid4()}),
        ("decision", {"plan_content_sha256": "f" * 64}),
        ("decision", {"content_sha256": "f" * 64}),
        ("plan", {"id": uuid4()}),
        ("plan", {"content_sha256": "f" * 64}),
        ("command", {"decision_id": uuid4()}),
        ("command", {"decision_content_sha256": "f" * 64}),
        ("command", {"expected_incident_version": 5}),
    ],
)
def test_content_rejects_mismatched_original_plan_decision_and_command(target, changes):
    plan = plan_for()
    decision = decision_for(plan)
    command = command_for(plan, decision)
    if target == "decision":
        decision = decision.model_copy(update=changes)
    elif target == "plan":
        plan = plan.model_copy(update=changes)
    else:
        command = command.model_copy(update=changes)
    with pytest.raises(ConditionError) as error:
        build_content(decision, plan, command)
    assert error.value.code == "candidate_condition_source_invalid"


def test_even_rehashed_changed_original_conditions_do_not_match_retained_decision():
    plan = plan_for()
    decision = decision_for(plan)
    altered = decision.content.model_copy(update={"preconditions": ("其他条件",)})
    decision = decision.model_copy(
        update={"content": altered, "content_sha256": content_hash(altered)}
    )
    with pytest.raises(ConditionError):
        build_content(decision, plan, command_for(plan, decision))


@pytest.mark.parametrize(
    "field,value", [("approval_required", False), ("execution_eligible", True)]
)
def test_content_and_state_cannot_claim_new_authorization(field, value):
    plan = plan_for()
    decision = decision_for(plan)
    content = build_content(decision, plan, command_for(plan, decision))
    with pytest.raises(ValidationError):
        ConditionContent.model_validate({**content.model_dump(), field: value})
    with pytest.raises(ValidationError):
        ConditionState.model_validate(
            {
                "catalog": build_catalog(plan),
                "condition_version": 0,
                "latest": None,
                "availability": "pending",
                field: value,
            }
        )


@pytest.mark.parametrize("model_kind", ["content", "state"])
@pytest.mark.parametrize("field", ["approval_required", "execution_eligible"])
@pytest.mark.parametrize("value", [0, 1, 0.0, 1.0, "true", "false", "0", "1"])
def test_authorization_flags_require_actual_json_booleans(model_kind, field, value):
    plan = plan_for()
    decision = decision_for(plan)
    if model_kind == "content":
        model = ConditionContent
        data = build_content(decision, plan, command_for(plan, decision)).model_dump()
    else:
        model = ConditionState
        data = {
            "catalog": build_catalog(plan),
            "condition_version": 0,
            "latest": None,
            "availability": "pending",
            "approval_required": True,
            "execution_eligible": False,
        }
    with pytest.raises(ValidationError, match="JSON布尔值"):
        model.model_validate({**data, field: value})
    checked = model.model_validate(data)
    assert checked.approval_required is True and checked.execution_eligible is False


def test_version_cas_accepts_only_exact_next_snapshot_and_keeps_history_after_expiry():
    plan = plan_for()
    decision = decision_for(plan)
    initial = command_for(plan, decision)
    validate_version(initial, None)
    latest = record_for(plan, decision, expires_at=NOW - timedelta(seconds=1))
    following = command_for(
        plan, decision, expected_condition_version=1, previous_content_sha256=latest.content_sha256
    )
    validate_version(following, latest)
    for command, predecessor in (
        (initial, latest),
        (following, None),
        (following.model_copy(update={"previous_content_sha256": "f" * 64}), latest),
        (following.model_copy(update={"expected_condition_version": 2}), latest),
        (following.model_copy(update={"decision_id": uuid4()}), latest),
        (following.model_copy(update={"decision_content_sha256": "f" * 64}), latest),
    ):
        with pytest.raises(ConditionError) as error:
            validate_version(command, predecessor)
        assert error.value.code == "candidate_condition_version_conflict"
    with pytest.raises(ConditionError) as error:
        validate_version(following, latest.model_copy(update={"version": 8}))
    assert error.value.code == "candidate_condition_version_limit"


@pytest.mark.parametrize(
    "latest,decision_status,seconds,budget,expected",
    [
        (2, "expired", 301, False, "superseded"),
        (1, "current", 300, True, "expired"),
        (1, "stale", 301, False, "expired"),
        (1, "expired", 0, True, "expired"),
        (1, "current", 299, True, "current"),
        (1, "current", 0, False, "stale"),
        (1, "stale", 0, True, "stale"),
        (1, "recorded", 0, True, "stale"),
        (1, "pending", 0, True, "stale"),
    ],
)
def test_availability_prioritizes_supersession_then_expiry_then_current_inputs(
    latest, decision_status, seconds, budget, expected
):
    plan = plan_for()
    decision = decision_for(plan)
    record = record_for(plan, decision)
    assert (
        verification_availability(
            record, latest, decision_status, NOW + timedelta(seconds=seconds), budget_current=budget
        )
        == expected
    )


@pytest.mark.parametrize(
    "deadline_seconds,observation_age,preflight_seconds,expected",
    [
        (600, None, (), 300),
        (60, None, (), 60),
        (600, 45, (), 255),
        (600, 45, (120, 240), 120),
        (60, 45, (120,), 60),
    ],
)
def test_deadline_is_minimum_of_original_request_snapshot_and_all_sources(
    deadline_seconds, observation_age, preflight_seconds, expected
):
    sources = (
        ()
        if observation_age is None
        else (
            HumanObservation(
                text="人工现场观察",
                observed_at=NOW - timedelta(seconds=observation_age),
            ),
        )
    )
    item = ConditionInput(
        condition_id="a" * 64, status="unresolved", explanation="需要继续调查", sources=sources
    )
    assert expires_at(
        NOW,
        NOW + timedelta(seconds=deadline_seconds),
        (item,),
        tuple(NOW + timedelta(seconds=value) for value in preflight_seconds),
    ) == (NOW + timedelta(seconds=expected))


def test_policy_rejects_naive_clock_and_returns_utc_deadline():
    plan = plan_for()
    decision = decision_for(plan)
    record = record_for(plan, decision)
    with pytest.raises(ValueError, match="时区"):
        verification_availability(
            record, 1, "current", NOW.replace(tzinfo=None), budget_current=True
        )
    with pytest.raises(ValueError, match="时区"):
        expires_at(NOW.replace(tzinfo=None), NOW, (), ())
    deadline = expires_at(
        NOW.astimezone(timezone(timedelta(hours=8))), NOW + timedelta(seconds=600), (), ()
    )
    assert deadline.tzinfo is UTC
