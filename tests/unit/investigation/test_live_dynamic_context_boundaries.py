"""现场协调只使用匹配版本、分钟窗口与同模式经验，历史路径保持兼容。"""

import json
from contextlib import asynccontextmanager
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from opsswarm.experience import context_store
from opsswarm.experience.context_contracts import ExperienceContextItem, ExperienceContextSelection
from opsswarm.experience.contracts import ExperienceSource, canonical_hash
from opsswarm.experience.lifecycle_contracts import ExperienceSearchResult
from opsswarm.investigation.context.contracts import ContextManifest
from opsswarm.investigation.coordination import persistence
from opsswarm.investigation.coordination.context import (
    _require_live_scope,
    build_coordination_context,
)
from opsswarm.investigation.coordination.contracts import (
    CoordinationPolicy,
    CoordinationPolicyRecord,
    CoordinationProposal,
    ProposedTask,
)
from opsswarm.investigation.coordination.hashing import coordination_hash
from opsswarm.investigation.coordination.policy import compile_coordination_plan
from opsswarm.investigation.handoff import persistence as handoff
from opsswarm.investigation.models import InvestigationDataSourceMode
from opsswarm.investigation.persistence.tables import InvestigationRunRow
from opsswarm.investigation.tasks.errors import TaskFactConflict
from opsswarm.investigation.tasks.evidence import live_snapshot_id
from tests.loop_helpers import loop_policy
from tests.task_facts_helpers import BASE_TIME
from tests.unit.experience.test_context_selection import (
    _frozen,
    _session,
)
from tests.unit.experience.test_context_selection import baseline as exported_baseline
from tests.unit.experience.test_lifecycle_policy import _entry_from_source
from tests.unit.investigation.test_coordination_policy import request

baseline = exported_baseline
LIVE_POLICY = "task-evidence-live-dynamic-mcp-v1"


def live_run() -> InvestigationRunRow:
    return InvestigationRunRow(
        id=uuid4(),
        workflow_version="v3",
        graph_version="dynamic-investigation-graph-v16",
        data_source_mode="live",
        evidence_policy_version=LIVE_POLICY,
        observation_cutoff_at=BASE_TIME,
    )


def context_packet(*, live=True, selection=None, started_at=None, cutoff=BASE_TIME):
    policy = CoordinationPolicy()
    run_id = selection.scope.run_id if selection is not None else uuid4()
    incident = request()
    if started_at is not None:
        incident = incident.model_copy(update={"started_at": started_at})
    if selection is not None:
        incident = incident.model_copy(
            update={
                "incident_id": selection.scope.incident_id,
                "service_name": selection.scope.service_name,
            }
        )
    record = CoordinationPolicyRecord(
        investigation_run_id=run_id,
        version=policy.version,
        policy=policy,
        content_sha256=coordination_hash(policy),
        created_at=BASE_TIME,
    )
    return build_coordination_context(
        incident,
        record,
        round_number=1,
        existing_task_count=0,
        observation_cutoff_at=cutoff,
        evidence_snapshot_id="test-snapshot",
        model_policy=loop_policy(),
        workflow_version="v3",
        evidence_policy_version=LIVE_POLICY if live else "task-evidence-v1",
        experience_selection=selection,
    )


def test_live_coordinator_payload_and_manifest_state_actual_window_policy():
    packet = context_packet()
    payload = json.loads(packet.model_input.messages[1].content)
    assert payload["lookback_seconds_multiple_of"] == 60
    assert payload["role_max_lookback_seconds"] == {"log": 900, "metric": 900, "change": 21600}
    assert "60倍数" in packet.model_input.messages[0].content
    assert packet.manifest.evidence_policy_version == LIVE_POLICY
    legacy = context_packet(live=False)
    assert "lookback_seconds_multiple_of" not in json.loads(legacy.model_input.messages[1].content)
    assert legacy.manifest.evidence_policy_version == "task-evidence-v1"


def test_live_coordinator_manifest_is_v3_only():
    payload = context_packet().manifest.model_dump(mode="python")
    payload["workflow_version"] = "v2"
    with pytest.raises(ValidationError):
        ContextManifest.model_validate(payload)


@pytest.mark.parametrize(("role", "seconds"), [("log", 60), ("metric", 900), ("change", 21600)])
def test_live_plan_accepts_precise_minute_windows(role, seconds):
    run = live_run()
    plan = compile_coordination_plan(
        CoordinationProposal(
            round_number=1,
            goal="检查现场",
            tasks=(
                ProposedTask(task_key=role, role=role, goal="精确取证", lookback_seconds=seconds),
            ),
        ),
        policy=CoordinationPolicy(),
        request=request(),
        expected_round_number=1,
        existing_task_count=0,
        observation_cutoff_at=BASE_TIME,
        evidence_snapshot_id=live_snapshot_id(run.id, BASE_TIME),
    )
    persistence._require_plan_assignments(run, plan.assignment.tasks)
    assert plan.assignment.tasks[0].window_start == BASE_TIME - timedelta(seconds=seconds)


@pytest.mark.asyncio
async def test_live_plan_rejects_nonminute_window_before_any_policy_or_task_write(monkeypatch):
    run = live_run()
    session = MagicMock(spec=AsyncSession)
    session.scalars = AsyncMock(return_value=[])
    session.scalar = AsyncMock(return_value=0)

    @asynccontextmanager
    async def transaction(*args):
        incident = request()
        yield (
            session,
            SimpleNamespace(id=uuid4()),
            SimpleNamespace(
                id=incident.incident_id, **incident.model_dump(exclude={"incident_id"})
            ),
            run,
            BASE_TIME,
        )

    monkeypatch.setattr(persistence, "active_investigation_transaction", transaction)
    save_round = AsyncMock()
    fix_policy = AsyncMock()
    monkeypatch.setattr(persistence, "create_round_in_transaction", save_round)
    monkeypatch.setattr(persistence, "_fix_policy", fix_policy)
    with pytest.raises(TaskFactConflict):
        await persistence.CoordinationPlanStore(MagicMock()).create_plan(
            run.id,
            lease_token=uuid4(),
            policy=CoordinationPolicy(),
            proposal=CoordinationProposal(
                round_number=1,
                goal="检查现场",
                tasks=(ProposedTask(task_key="log", role="log", goal="取证", lookback_seconds=61),),
            ),
            observation_cutoff_at=BASE_TIME,
            evidence_snapshot_id=live_snapshot_id(run.id, BASE_TIME),
        )
    fix_policy.assert_not_awaited()
    save_round.assert_not_awaited()


def test_frozen_plan_preserves_second_precision():
    run = live_run()
    run.graph_version = "dynamic-investigation-graph-v15"
    run.data_source_mode = "frozen"
    run.evidence_policy_version = "task-evidence-v1"
    plan = compile_coordination_plan(
        CoordinationProposal(
            round_number=1,
            goal="历史秒级窗口",
            tasks=(ProposedTask(task_key="log", role="log", goal="取证", lookback_seconds=61),),
        ),
        policy=CoordinationPolicy(),
        request=request(),
        expected_round_number=1,
        existing_task_count=0,
        observation_cutoff_at=BASE_TIME,
        evidence_snapshot_id="frozen-snapshot",
    )
    persistence._require_plan_assignments(run, plan.assignment.tasks)


@pytest.mark.parametrize(
    ("graph", "mode", "policy"),
    [
        ("dynamic-investigation-graph-v15", "live", LIVE_POLICY),
        ("dynamic-investigation-graph-v16", "frozen", "task-evidence-v1"),
        ("dynamic-investigation-graph-v16", "live", "task-evidence-live-mcp-v1"),
    ],
)
def test_coordination_plan_rejects_cross_source_versions(graph, mode, policy):
    run = live_run()
    run.graph_version, run.data_source_mode, run.evidence_policy_version = graph, mode, policy
    with pytest.raises(TaskFactConflict):
        persistence._require_plan_assignments(run, ())


def _make_live_baseline(baseline):
    baseline.run.graph_version = "dynamic-investigation-graph-v16"
    baseline.run.data_source_mode = "live"
    baseline.run.evidence_policy_version = LIVE_POLICY
    data = baseline.selection.model_dump(mode="json")
    data["scope"]["data_source_mode"] = "live"
    data["items"] = []
    baseline.selection = ExperienceContextSelection.model_validate(data)
    return baseline


@pytest.mark.asyncio
async def test_live_experience_empty_selection_is_frozen_and_replayed_without_new_search(
    baseline, monkeypatch
):
    baseline = _make_live_baseline(baseline)
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
    selected = await context_store.validate_experience_selection(
        session,
        baseline.run,
        baseline.selection,
        first_call_id=baseline.first_call_id,
        reserved_at=baseline.reserved_at,
    )
    assert selected.scope.data_source_mode == "live"
    assert session.add.call_args.args[0].content["items"] == []
    row = _frozen(baseline)
    baseline.call.context_manifest["evidence_policy_version"] = LIVE_POLICY
    search.reset_mock()
    search.side_effect = AssertionError("历史恢复不得重新检索")
    assert await context_store.prepare_experience_selection(
        _session(baseline, row), baseline.run
    ) == (selected)
    search.assert_not_awaited()
    packet = context_packet(selection=selected)
    assert packet.manifest.evidence_policy_version == LIVE_POLICY
    assert packet.manifest.version == "coordination-experience-v1"


@pytest.mark.asyncio
async def test_live_experience_refuses_frozen_manifest_replay(baseline):
    baseline = _make_live_baseline(baseline)
    row = _frozen(baseline)
    baseline.call.context_manifest["evidence_policy_version"] = "task-evidence-v1"
    with pytest.raises(TaskFactConflict):
        await context_store.load_experience_selection(_session(baseline, row), baseline.run)


@pytest.mark.asyncio
async def test_live_handoff_keeps_highest_completed_round_and_pending_call_guards(monkeypatch):
    run = live_run()
    route_id, round_id = uuid4(), uuid4()
    row = SimpleNamespace(investigation_run_id=run.id, content_sha256="digest")
    route = SimpleNamespace(round_id=round_id, decision=SimpleNamespace(route="await_human"))
    session = MagicMock(spec=AsyncSession)
    session.get = AsyncMock(return_value=row)
    session.scalars = AsyncMock(return_value=[SimpleNamespace(id=round_id, status="completed")])
    session.scalar = AsyncMock(side_effect=[None, None])
    monkeypatch.setattr(handoff, "validated_review_routing", AsyncMock(return_value=route))
    assert await handoff.terminal_source(session, run, route_id, "digest") == route
    session.scalar.side_effect = [None, uuid4()]
    with pytest.raises(TaskFactConflict, match="无未完成工作"):
        await handoff.terminal_source(session, run, route_id, "digest")


@pytest.mark.parametrize("drift", ["cutoff", "snapshot"])
def test_live_coordination_rejects_scope_drift_before_model_reservation(drift):
    run = live_run()
    cutoff = BASE_TIME + timedelta(seconds=1) if drift == "cutoff" else BASE_TIME
    snapshot = "other-snapshot" if drift == "snapshot" else live_snapshot_id(run.id, cutoff)
    with pytest.raises(TaskFactConflict):
        _require_live_scope(run, cutoff, snapshot)
    _require_live_scope(run, BASE_TIME, live_snapshot_id(run.id, BASE_TIME))


@pytest.mark.parametrize(
    ("live", "seconds_before_start", "accepted"),
    [(True, 0, True), (True, 1, False), (False, 0, False)],
)
def test_microsecond_incident_start_only_allows_same_second_cutoff_in_live_contract(
    live, seconds_before_start, accepted
):
    started_at = BASE_TIME.replace(microsecond=654321)
    cutoff = BASE_TIME - timedelta(seconds=seconds_before_start)
    incident = request().model_copy(update={"started_at": started_at})
    run = live_run()
    if not live:
        run.graph_version = "dynamic-investigation-graph-v15"
        run.data_source_mode = "frozen"
        run.evidence_policy_version = "task-evidence-v1"

    def compile_plan():
        return compile_coordination_plan(
            CoordinationProposal(
                round_number=1,
                goal="检查微秒起点",
                tasks=(ProposedTask(task_key="log", role="log", goal="取证", lookback_seconds=60),),
            ),
            policy=CoordinationPolicy(),
            request=incident,
            expected_round_number=1,
            existing_task_count=0,
            observation_cutoff_at=cutoff,
            evidence_snapshot_id="test-snapshot",
            allow_partial_start_second=persistence._allow_partial_start_second(run),
        )

    if accepted:
        assert compile_plan().assignment.tasks[0].window_end == cutoff
        assert context_packet(live=live, started_at=started_at, cutoff=cutoff)
    else:
        with pytest.raises(TaskFactConflict):
            compile_plan()
        with pytest.raises(TaskFactConflict):
            context_packet(live=live, started_at=started_at, cutoff=cutoff)


def test_default_compiler_keeps_frozen_microsecond_boundary_strict():
    with pytest.raises(TaskFactConflict):
        compile_coordination_plan(
            CoordinationProposal(
                round_number=1,
                goal="冻结兼容",
                tasks=(ProposedTask(task_key="log", role="log", goal="取证", lookback_seconds=60),),
            ),
            policy=CoordinationPolicy(),
            request=request().model_copy(update={"started_at": BASE_TIME.replace(microsecond=1)}),
            expected_round_number=1,
            existing_task_count=0,
            observation_cutoff_at=BASE_TIME,
            evidence_snapshot_id="test-snapshot",
        )


def test_nonempty_live_experience_enters_actual_coordinator_messages_with_exact_versions(baseline):
    # 仅将既有纯来源夹具投影为现场模式；不执行关闭链或数据库来源查询。
    source = baseline.source
    investigation = source.investigation.model_copy(
        update={"data_source_mode": InvestigationDataSourceMode.LIVE}
    )
    workroom = source.report.content.workroom.model_copy(update={"investigation": investigation})
    report_content = source.report.content.model_copy(update={"workroom": workroom})
    report_hash = canonical_hash(report_content)
    report = source.report.model_copy(
        update={"content": report_content, "content_hash": report_hash}
    )
    live_source = ExperienceSource.model_validate(
        source.model_copy(
            update={
                "data_source_mode": InvestigationDataSourceMode.LIVE,
                "investigation": investigation,
                "report": report,
                "report_content_hash": report_hash,
            }
        ).model_dump(mode="python")
    )
    entry = _entry_from_source(live_source)
    baseline = _make_live_baseline(baseline)
    selection_data = baseline.selection.model_dump(mode="python")
    selection_data["items"] = (
        ExperienceContextItem(source_incident_id=live_source.incident_id, entry=entry),
    )
    selection = ExperienceContextSelection.model_validate(selection_data)
    packet = context_packet(selection=selection)
    payload = json.loads(packet.model_input.messages[1].content)

    assert payload["historical_experience"] == selection.model_dump(mode="json")
    supplied = payload["historical_experience"]["items"][0]["entry"]
    assert supplied["content"] == entry.content.model_dump(mode="json")
    assert supplied["version"] == entry.version
    assert supplied["content_hash"] == entry.content_hash
    assert supplied["source_hash"] == entry.source_hash
    assert entry.content.candidate_snapshot.data_source_mode == "live"
    reference = packet.manifest.materials[3]
    assert reference.kind == "confirmed_experience"
    assert reference.material_id == entry.id
    assert reference.version == str(entry.version)
    assert reference.content_sha256 == canonical_hash(entry.content)
    assert reference.evidence_ids == ()
    assert packet.manifest.evidence_policy_version == LIVE_POLICY


def test_frozen_confirmed_experience_cannot_enter_live_selection_or_packet(baseline):
    data = baseline.selection.model_dump(mode="python")
    data["scope"]["data_source_mode"] = "live"
    with pytest.raises(ValidationError, match="经验上下文与系统范围"):
        ExperienceContextSelection.model_validate(data)
    with pytest.raises(TaskFactConflict, match="协调经验范围"):
        context_packet(selection=baseline.selection)
