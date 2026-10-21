"""真实导出来源上的经验选择冻结与历史恢复纯单元验收。"""

import json
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid5

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from opsswarm.experience import context_store
from opsswarm.experience.artifact_validation import candidate_id_for_job
from opsswarm.experience.context_contracts import ExperienceContextItem, ExperienceContextSelection
from opsswarm.experience.context_tables import ExperienceContextRow
from opsswarm.experience.contracts import (
    GENERATION_POLICY_VERSION,
    ExperienceSource,
    canonical_hash,
)
from opsswarm.experience.lifecycle_contracts import ExperienceSearchResult, SearchScope
from opsswarm.experience.projection import project_source
from opsswarm.experience.review_contracts import ConfirmedExperienceContent, EntryView
from opsswarm.experience.review_validation import entry_id_for_candidate, review_id_for_candidate
from opsswarm.experience.search_policy import summarize_entry
from opsswarm.experience.tables import ExperienceCandidateRow, ExperienceJobRow
from opsswarm.incidents.persistence.tables import IncidentRow
from opsswarm.investigation.budget.tables import CallReservationRow
from opsswarm.investigation.persistence.tables import InvestigationRunRow
from opsswarm.investigation.tasks.errors import TaskFactConflict

POLICY = "coordination-experience-v1"


@pytest.fixture
def baseline() -> SimpleNamespace:
    source = ExperienceSource.model_validate_json(
        (Path(__file__).resolve().parents[2] / "fixtures/experience-source-v1.json").read_text(
            encoding="utf-8"
        )
    )
    draft = project_source(source)
    candidate_id = candidate_id_for_job(uuid5(source.report_id, "context-unit-job"))
    content = ConfirmedExperienceContent(candidate_snapshot=draft)
    entry = EntryView(
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
    run = InvestigationRunRow(
        id=uuid5(entry.id, "target-run"),
        incident_id=uuid5(source.incident_id, "target-incident"),
        coordination_experience_policy_version=POLICY,
        workflow_version="v3",
        graph_version="dynamic-investigation-graph-v15",
        data_source_mode="frozen",
        mode="multi_agent",
        created_at=entry.created_at + timedelta(seconds=1),
    )
    incident = IncidentRow(id=run.incident_id, service_name=draft.service_name)
    scope = SearchScope(
        run_id=run.id,
        incident_id=run.incident_id,
        service_name=draft.service_name,
        data_source_mode="frozen",
    )
    item = ExperienceContextItem(source_incident_id=source.incident_id, entry=entry)
    selected_at = run.created_at + timedelta(seconds=1)
    selection = ExperienceContextSelection(
        id=uuid5(run.id, POLICY), scope=scope, selected_at=selected_at, items=(item,)
    )
    return SimpleNamespace(
        source=source,
        run=run,
        incident=incident,
        item=item,
        selection=selection,
        reserved_at=selected_at + timedelta(seconds=1),
        first_call_id=uuid5(run.id, "first-call"),
    )


def _session(baseline: SimpleNamespace, row: ExperienceContextRow | None = None) -> MagicMock:
    session = MagicMock(spec=AsyncSession)
    session.get = AsyncMock(
        side_effect=lambda table, key: (
            row
            if table is ExperienceContextRow
            else baseline.incident
            if table is IncidentRow
            else baseline.call
            if table is CallReservationRow
            else None
        )
    )
    session.flush = AsyncMock()
    return session


def _frozen(baseline: SimpleNamespace, *, status: str = "failed") -> ExperienceContextRow:
    selection = baseline.selection

    def material(kind: str, identity: object, version: str, digest: str) -> dict[str, object]:
        return {
            "kind": kind,
            "material_id": str(identity),
            "version": version,
            "content_sha256": digest,
            "evidence_ids": [],
        }

    baseline.call = CallReservationRow(
        id=baseline.first_call_id,
        investigation_run_id=baseline.run.id,
        task_id=None,
        task_attempt_id=None,
        invocation_index=0,
        kind="model",
        operation="coordinate",
        status=status,
        reserved_at=baseline.reserved_at,
        model_input={
            "version": "coordination-experience-input-v1",
            "messages": [
                {"role": "system", "content": "协调提示"},
                {
                    "role": "user",
                    "content": json.dumps(
                        {"historical_experience": selection.model_dump(mode="json")}
                    ),
                },
            ],
        },
        context_manifest={
            "version": POLICY,
            "investigation_run_id": str(baseline.run.id),
            "task_id": None,
            "role": "coordinator",
            "role_version": "dynamic-coordinator-experience-v1",
            "prompt_version": "coordination-experience-plan-v1",
            "workflow_version": "v3",
            "tool_names": [],
            "materials": [
                material("experience_selection", selection.id, POLICY, canonical_hash(selection)),
                *[
                    material("confirmed_experience", item.entry.id, "1", item.entry.content_hash)
                    for item in selection.items
                ],
            ],
        },
    )
    return ExperienceContextRow(
        investigation_run_id=baseline.run.id,
        id=selection.id,
        policy_version=POLICY,
        first_call_id=baseline.first_call_id,
        content=selection.model_dump(mode="json"),
        content_hash=canonical_hash(selection),
        created_at=baseline.reserved_at,
    )


def _current(monkeypatch: pytest.MonkeyPatch, baseline: SimpleNamespace) -> AsyncMock:
    search = AsyncMock(
        return_value=ExperienceSearchResult(
            scope=baseline.selection.scope,
            preferred_fault_tag=None,
            retrieved_at=baseline.selection.selected_at,
            items=(
                summarize_entry(
                    baseline.item.entry,
                    baseline.item.source_incident_id,
                    baseline.selection.scope,
                ),
            ),
        )
    )
    monkeypatch.setattr(context_store, "search_in_session", search)
    monkeypatch.setattr(context_store, "_item", AsyncMock(return_value=baseline.item))
    return search


@pytest.mark.asyncio
async def test_prepare_is_transient_and_uses_proposal_clock(
    baseline: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _session(baseline)
    search = _current(monkeypatch, baseline)
    result = await context_store.prepare_experience_selection(
        session, baseline.run, clock=lambda: baseline.selection.selected_at
    )

    assert result == baseline.selection
    assert search.call_args.kwargs["clock"]() == baseline.selection.selected_at
    session.add.assert_not_called()
    session.flush.assert_not_awaited()


@pytest.mark.asyncio
async def test_first_freeze_keeps_proposal_time_distinct_from_reservation(
    baseline: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _session(baseline)
    search = _current(monkeypatch, baseline)
    result = await context_store.validate_experience_selection(
        session,
        baseline.run,
        baseline.selection,
        first_call_id=baseline.first_call_id,
        reserved_at=baseline.reserved_at,
    )
    row = session.add.call_args.args[0]

    assert result == baseline.selection
    assert row.created_at == baseline.reserved_at > result.selected_at
    assert row.first_call_id == baseline.first_call_id
    assert row.content_hash == canonical_hash(result)
    assert search.call_args.kwargs["clock"]() == result.selected_at
    session.flush.assert_awaited_once()
    session.commit.assert_not_called()


@pytest.mark.asyncio
async def test_first_freeze_requires_actual_reservation_identity(baseline: SimpleNamespace) -> None:
    with pytest.raises(TaskFactConflict, match="首次模型预留"):
        await context_store.validate_experience_selection(
            _session(baseline), baseline.run, baseline.selection
        )


@pytest.mark.asyncio
async def test_first_freeze_rejects_caller_dropping_selected_material(
    baseline: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    _current(monkeypatch, baseline)
    data = baseline.selection.model_dump(mode="json")
    data["items"] = []
    reduced = ExperienceContextSelection.model_validate(data)
    session = _session(baseline)

    with pytest.raises(TaskFactConflict, match="有效集合、顺序或工件"):
        await context_store.validate_experience_selection(
            session,
            baseline.run,
            reduced,
            first_call_id=baseline.first_call_id,
            reserved_at=baseline.reserved_at,
        )

    session.add.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["reserved", "succeeded", "failed", "unknown"])
async def test_history_reuses_frozen_material_without_active_search(
    baseline: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    row = _frozen(baseline, status=status)
    search = AsyncMock(side_effect=AssertionError("历史读取不能重新检索有效状态"))
    monkeypatch.setattr(context_store, "search_in_session", search)
    monkeypatch.setattr(context_store, "_item", AsyncMock(return_value=baseline.item))

    assert (
        await context_store.prepare_experience_selection(_session(baseline, row), baseline.run)
        == baseline.selection
    )
    search.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["content_hash", "id", "policy_version", "created_at"])
async def test_history_rejects_snapshot_identity_hash_or_time_drift(
    baseline: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    row = _frozen(baseline)
    setattr(
        row,
        field,
        {
            "content_hash": "0" * 64,
            "id": uuid5(row.id, "wrong"),
            "policy_version": "unsupported",
            "created_at": baseline.run.created_at,
        }[field],
    )
    monkeypatch.setattr(context_store, "_item", AsyncMock(return_value=baseline.item))

    with pytest.raises(TaskFactConflict, match="身份、范围、时间或散列"):
        await context_store.load_experience_selection(_session(baseline, row), baseline.run)


@pytest.mark.asyncio
@pytest.mark.parametrize("drift", ["payload", "manifest_hash", "current_evidence", "call_run"])
async def test_history_rejects_first_call_input_or_manifest_drift(
    baseline: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, drift: str
) -> None:
    row = _frozen(baseline)
    if drift == "payload":
        baseline.call.model_input["messages"][1]["content"] = "{}"
    elif drift == "manifest_hash":
        baseline.call.context_manifest["materials"][0]["content_sha256"] = "0" * 64
    elif drift == "current_evidence":
        baseline.call.context_manifest["materials"][1]["evidence_ids"] = [
            str(baseline.source.evidence[0].id)
        ]
    else:
        baseline.call.investigation_run_id = uuid5(baseline.run.id, "other-run")
    monkeypatch.setattr(context_store, "_item", AsyncMock(return_value=baseline.item))

    with pytest.raises(TaskFactConflict):
        await context_store.load_experience_selection(_session(baseline, row), baseline.run)


@pytest.mark.asyncio
async def test_existing_freeze_rejects_incoming_selection_reduction(
    baseline: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    row = _frozen(baseline)
    monkeypatch.setattr(context_store, "_item", AsyncMock(return_value=baseline.item))
    reduced = ExperienceContextSelection.model_validate(
        {**baseline.selection.model_dump(mode="json"), "items": []}
    )

    with pytest.raises(TaskFactConflict, match="已冻结"):
        await context_store.validate_experience_selection(
            _session(baseline, row), baseline.run, reduced
        )


@pytest.mark.asyncio
async def test_source_errors_are_stable_and_do_not_expose_material(
    baseline: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        context_store, "search_in_session", AsyncMock(side_effect=ValueError("秘密原始材料"))
    )
    with pytest.raises(TaskFactConflict) as error:
        await context_store.prepare_experience_selection(
            _session(baseline), baseline.run, clock=lambda: baseline.selection.selected_at
        )

    assert error.value.code == "experience_context_source_conflict"
    assert "秘密" not in str(error.value)


@pytest.mark.asyncio
async def test_empty_selection_is_frozen_and_later_reused_without_search(
    baseline: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    baseline.selection = ExperienceContextSelection.model_validate(
        {**baseline.selection.model_dump(mode="json"), "items": []}
    )
    search = AsyncMock(
        return_value=ExperienceSearchResult(
            scope=baseline.selection.scope,
            preferred_fault_tag=None,
            retrieved_at=baseline.selection.selected_at,
            items=(),
        )
    )
    monkeypatch.setattr(context_store, "search_in_session", search)
    session = _session(baseline)
    await context_store.validate_experience_selection(
        session,
        baseline.run,
        baseline.selection,
        first_call_id=baseline.first_call_id,
        reserved_at=baseline.reserved_at,
    )
    assert session.add.call_args.args[0].content["items"] == []
    row = _frozen(baseline)
    search.reset_mock()

    assert (
        await context_store.load_experience_selection(_session(baseline, row), baseline.run)
        == baseline.selection
    )
    search.assert_not_awaited()


@pytest.mark.asyncio
async def test_item_uses_immutable_job_source_incident_without_live_report(
    baseline: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = baseline.item.entry
    job = ExperienceJobRow(
        id=uuid5(entry.id, "source-job"),
        report_id=baseline.source.report_id,
        source=baseline.source.model_dump(mode="json"),
        source_hash=canonical_hash(baseline.source),
        policy_version=GENERATION_POLICY_VERSION,
        max_attempts=3,
    )
    candidate = ExperienceCandidateRow(id=entry.candidate_id, job_id=job.id)
    session = MagicMock(spec=AsyncSession)
    rows = {
        (ExperienceCandidateRow, candidate.id): candidate,
        (ExperienceJobRow, job.id): job,
    }
    session.get = AsyncMock(side_effect=lambda table, key: rows[(table, key)])
    monkeypatch.setattr(
        context_store, "load_confirmed_entry_in_session", AsyncMock(return_value=entry)
    )

    result = await context_store._item(session, entry.id)

    assert result.source_incident_id == baseline.source.incident_id
    assert result.entry == entry
    assert session.get.await_count == 2
