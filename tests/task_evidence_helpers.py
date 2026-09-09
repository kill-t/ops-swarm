"""专业取证快照的固定材料与真实数据库准备。"""

from opsswarm.investigation.budget.contracts import CallOperation
from opsswarm.investigation.budget.store import CallBudgetStore
from opsswarm.investigation.models import EvidenceKind, SpecialistReport
from opsswarm.investigation.scenarios import RELEASE_500_SCENARIO, FrozenScenarioDataSource
from opsswarm.investigation.tasks.evidence import TaskEvidenceSnapshot
from opsswarm.investigation.tasks.evidence_store import TaskEvidenceStore
from opsswarm.investigation.tasks.queries import CollaborationQueries
from opsswarm.investigation.tasks.results import SpecialistTaskResult
from opsswarm.investigation.tasks.runtime import FixedTaskRuntime
from opsswarm.investigation.tasks.store import InvestigationTaskStore
from opsswarm.investigation.workflow_versions import WorkflowVersion
from tests.task_facts_helpers import TaskClock, claimed_run


def material():
    source = FrozenScenarioDataSource(RELEASE_500_SCENARIO)
    return source.query_logs(service_name="order-service", lookback_minutes=15)


def professional_result(assignment, evidence=None):
    evidence = material() if evidence is None else evidence
    return SpecialistTaskResult(
        evidence_snapshot_id=assignment.evidence_snapshot_id,
        evidence=evidence,
        report=SpecialistReport(
            specialist=EvidenceKind.LOG,
            summary="完成日志调查",
            findings=("读取了日志",),
            evidence_ids=tuple(e.evidence_id for e in evidence),
            confidence=0.9,
        ),
    )


async def evidence_attempt(
    db, *, call_status="succeeded", model_budget=None, context_policy_version=None, call_budget=None
):
    parent, claim = await claimed_run(
        db,
        workflow_version=WorkflowVersion.V2,
        model_budget=model_budget,
        context_policy_version=context_policy_version,
        call_budget=call_budget,
    )
    clock = TaskClock()
    tasks, queries = InvestigationTaskStore(db, clock=clock), CollaborationQueries(db)
    source = FrozenScenarioDataSource(RELEASE_500_SCENARIO)
    runtime = FixedTaskRuntime(
        run_id=claim.run.id,
        lease_token=claim.work_item.lease_token,
        store=tasks,
        queries=queries,
        snapshot_id=source.snapshot_id,
        snapshot_time=source.snapshot_time,
    )
    await runtime.prepare(claim.request)
    task = next(
        t
        for t in (await queries.get_snapshot(claim.run.id)).rounds[0].tasks
        if t.assignment.role is EvidenceKind.LOG
    )
    started = await tasks.begin_attempt(
        claim.run.id,
        task.id,
        lease_token=claim.work_item.lease_token,
        expected_version=task.version,
    )
    budget = CallBudgetStore(db, clock=clock)
    grant = await budget.reserve(
        claim.run.id,
        lease_token=claim.work_item.lease_token,
        operation=CallOperation.QUERY_LOGS,
        task_id=task.id,
        task_attempt_id=started.attempt.id,
    )
    if call_status != "reserved":
        await budget.settle(
            claim.run.id,
            grant.record.id,
            lease_token=claim.work_item.lease_token,
            status=call_status,
            error_code=None if call_status == "succeeded" else "operation_failed",
        )
    return parent, claim, clock, tasks, task, started.attempt, TaskEvidenceStore(db, clock=clock)


async def save_evidence(prepared, evidence=None):
    _, claim, _, _, task, attempt, store = prepared
    return await store.save(
        claim.run.id,
        task.id,
        attempt.id,
        lease_token=claim.work_item.lease_token,
        evidence=material() if evidence is None else evidence,
    )


def snapshot():
    from datetime import timedelta

    source = FrozenScenarioDataSource(RELEASE_500_SCENARIO)
    return TaskEvidenceSnapshot(
        evidence_snapshot_id=source.snapshot_id,
        role="log",
        service_name="order-service",
        window_start=source.snapshot_time - timedelta(minutes=15),
        window_end=source.snapshot_time,
        evidence=material(),
    )
