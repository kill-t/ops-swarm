"""人工命令的有界契约、精确等待版本与可信来源规则。"""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4, uuid5

import pytest
from pydantic import ValidationError

from opsswarm.identity.tables import OperatorAuthorizationRow
from opsswarm.investigation.context.hashing import content_hash
from opsswarm.investigation.handoff.contracts import TerminalHandoffRecord
from opsswarm.investigation.human_wait.authorization import (
    operation_for,
    parse_command,
    route_for,
    validate_admission,
)
from opsswarm.investigation.human_wait.contracts import (
    CancelWaitCommand,
    HumanInputItem,
    HumanWaitError,
    HumanWaitEventRecord,
    SupplementWaitCommand,
)
from opsswarm.investigation.human_wait.policy import evaluate_wait_command
from opsswarm.investigation.human_wait.validation import project_wait_state

NOW = datetime(2026, 10, 21, 0, 0, tzinfo=UTC)
RUN_ID = UUID("d53c74b3-53f7-4acf-a586-a0f16a9e444a")
SUBJECT = "engineer-test"


def handoff() -> TerminalHandoffRecord:
    return TerminalHandoffRecord(
        id=uuid5(RUN_ID, "terminal-handoff:v1"),
        investigation_run_id=RUN_ID,
        investigation_attempt_id=uuid4(),
        source_review_route_id=uuid4(),
        source_sha256="a" * 64,
        destination="await_human",
        created_at=NOW,
    )


def supplement(source: TerminalHandoffRecord, **changes) -> SupplementWaitCommand:
    return SupplementWaitCommand.model_validate(
        {
            "handoff_id": source.id,
            "source_sha256": source.source_sha256,
            "expected_wait_version": 1,
            "idempotency_key": "unit-supplement",
            "reason": "补充人工调查线索",
            "items": [{"kind": "statement", "text": "工程师观察到错误率升高"}],
            **changes,
        }
    )


def event(source, command, *, created_at=NOW) -> HumanWaitEventRecord:
    version = command.expected_wait_version + 1
    return HumanWaitEventRecord(
        id=uuid5(RUN_ID, f"human-wait:{version}"),
        investigation_run_id=RUN_ID,
        handoff_id=source.id,
        state_version=version,
        action=command.action,
        subject_id=SUBJECT,
        authorization_id=uuid4(),
        command=command,
        command_sha256=content_hash(command),
        created_at=created_at,
    )


def evaluate(source, command, *, events=(), existing=None, subject=SUBJECT, now=NOW):
    return evaluate_wait_command(
        source,
        project_wait_state(source, events),
        command,
        subject_id=subject,
        existing=existing,
        now=now,
    )


@pytest.mark.parametrize("version", [True, "1", 1.0])
def test_wait_version_requires_integer_without_coercion(version) -> None:
    with pytest.raises(ValidationError):
        supplement(handoff(), expected_wait_version=version)


@pytest.mark.parametrize(
    "changes",
    [
        {"reason": " \t\n"},
        {"items": []},
        {"items": [{"kind": "statement", "text": "陈述"}] * 6},
        {"items": [{"kind": "tool_evidence", "text": "不允许冒充证据"}]},
        {"items": [{"kind": "statement", "text": " \n\t"}]},
        {"items": [{"kind": "constraint", "text": "限制", "observed_at": NOW}]},
    ],
)
def test_wait_command_rejects_unbounded_or_ambiguous_material(changes) -> None:
    with pytest.raises(ValidationError):
        supplement(handoff(), **changes)


def test_material_normalizes_text_and_explicit_timezone() -> None:
    item = HumanInputItem(
        kind="statement",
        text="  人工观察  ",
        observed_at="2026-10-21T08:00:00+08:00",
    )
    assert item.text == "人工观察"
    assert item.observed_at == NOW
    assert item.observed_at.tzinfo == UTC
    with pytest.raises(ValidationError):
        HumanInputItem(kind="statement", text="人工观察", observed_at=NOW.replace(tzinfo=None))


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"handoff_id": uuid4()}, "human_wait_source_conflict"),
        ({"source_sha256": "b" * 64}, "human_wait_source_conflict"),
        ({"expected_wait_version": 2}, "human_wait_version_conflict"),
    ],
)
def test_new_command_requires_exact_wait_source_and_version(changes, code) -> None:
    source = handoff()
    with pytest.raises(HumanWaitError) as caught:
        evaluate(source, supplement(source, **changes))
    assert caught.value.code == code


def test_old_success_replays_after_cancel_without_advancing_version() -> None:
    source = handoff()
    command = supplement(source)
    saved = event(source, command)
    cancel = CancelWaitCommand(
        handoff_id=source.id,
        source_sha256=source.source_sha256,
        expected_wait_version=2,
        idempotency_key="unit-cancellation",
        reason="停止本次调查",
    )
    events = (saved, event(source, cancel))
    assert project_wait_state(source, events).status == "cancelled"
    assert evaluate(source, command, events=events, existing=saved) == "replay"
    with pytest.raises(HumanWaitError) as caught:
        evaluate(source, supplement(source, expected_wait_version=3), events=events)
    assert caught.value.code == "human_wait_cancelled"


@pytest.mark.parametrize("different_subject", [False, True])
def test_existing_key_binds_complete_command_and_subject(different_subject) -> None:
    source = handoff()
    original = supplement(source)
    with pytest.raises(HumanWaitError) as caught:
        evaluate(
            source,
            original if different_subject else supplement(source, reason="篡改原始原因"),
            existing=event(source, original),
            subject="another-engineer" if different_subject else SUBJECT,
        )
    assert caught.value.code == "human_wait_idempotency_conflict"


def test_fifteen_supplements_reserve_the_last_slot_for_cancel() -> None:
    source = handoff()
    events = tuple(
        event(source, supplement(source, expected_wait_version=index + 1)) for index in range(15)
    )
    command = supplement(source, expected_wait_version=16)
    with pytest.raises(HumanWaitError) as caught:
        evaluate(source, command, events=events)
    assert caught.value.code == "human_wait_input_limit"
    cancel = CancelWaitCommand.model_validate(
        {key: value for key, value in command.model_dump().items() if key != "items"}
        | {"action": "cancel"}
    )
    assert evaluate(source, cancel, events=events) == "apply"
    assert project_wait_state(source, (*events, event(source, cancel))).state_version == 17


def test_future_observation_and_regressing_command_time_are_rejected() -> None:
    source = handoff()
    future = supplement(
        source,
        items=[{"kind": "statement", "text": "观察", "observed_at": NOW + timedelta(seconds=1)}],
    )
    with pytest.raises(HumanWaitError) as caught:
        evaluate(source, future)
    assert caught.value.code == "human_wait_future_observation"
    with pytest.raises(HumanWaitError) as caught:
        evaluate(source, supplement(source), now=NOW - timedelta(microseconds=1))
    assert caught.value.code == "human_wait_time_conflict"


def admission_for(command, raw_body: str) -> OperatorAuthorizationRow:
    return OperatorAuthorizationRow(
        id=uuid4(),
        policy_version="operator-auth-v1",
        subject_id=SUBJECT,
        credential_id="unit-credential",
        role="engineer",
        permissions=["read", "manage_incident"],
        operation=operation_for(command.action),
        method="POST",
        route=route_for(RUN_ID, command.action),
        resources={"run_id": str(RUN_ID)},
        body_sha256=hashlib.sha256(raw_body.encode()).hexdigest(),
        authorized_at=NOW,
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("policy_version", "operator-auth-v2"),
        ("subject_id", "another-engineer"),
        ("role", "approver"),
        ("method", "GET"),
        ("operation", "cancel_investigation_wait"),
        ("route", f"/api/v1/investigations/{RUN_ID}/human-wait/supplements"),
        ("resources", {"run_id": str(uuid4())}),
        ("permissions", ["read"]),
        ("body_sha256", "b" * 64),
        ("authorized_at", NOW + timedelta(microseconds=1)),
    ],
)
def test_historical_admission_rejects_forged_origin(field, value) -> None:
    command = supplement(handoff())
    raw = command.model_dump_json()
    admission = admission_for(command, raw)
    setattr(admission, field, value)
    with pytest.raises(HumanWaitError) as caught:
        validate_admission(
            admission,
            run_id=RUN_ID,
            subject_id=SUBJECT,
            command=command,
            raw_body=raw,
            occurred_at=NOW,
        )
    assert caught.value.code == "human_wait_integrity_violation"


def test_admission_accepts_equivalent_raw_body_but_rejects_other_full_command() -> None:
    source = handoff()
    payload = {
        "handoff_id": str(source.id),
        "source_sha256": source.source_sha256,
        "expected_wait_version": 1,
        "idempotency_key": "unit-supplement",
        "reason": "  补充人工调查线索  ",
        "items": [{"kind": "statement", "text": " 工程师观察到错误率升高 "}],
    }
    raw = json.dumps(payload, ensure_ascii=False, indent=2)
    command = supplement(source)
    assert parse_command(command.model_dump()) == command
    validate_admission(
        admission_for(command, raw),
        run_id=RUN_ID,
        subject_id=SUBJECT,
        command=command,
        raw_body=raw,
        occurred_at=NOW,
    )
    assert route_for(uuid4(), "supplement") == route_for(RUN_ID, "supplement")
    with pytest.raises(HumanWaitError):
        validate_admission(
            admission_for(command, raw),
            run_id=RUN_ID,
            subject_id=SUBJECT,
            command=supplement(source, reason="不同的完整命令"),
            raw_body=raw,
            occurred_at=NOW,
        )
