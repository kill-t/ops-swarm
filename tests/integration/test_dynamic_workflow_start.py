"""真实 PostgreSQL 验证 v3 启动策略冻结与旧 Worker 隔离。"""

from datetime import timedelta

import pytest

from opsswarm.investigation.coordination.contracts import CoordinationPolicy
from opsswarm.investigation.coordination.queries import CoordinationPlanQueries
from opsswarm.investigation.models import InvestigationRunStatus, WorkItemStatus
from opsswarm.investigation.workflow_versions import WorkflowVersion
from tests.loop_helpers import loop_policy
from tests.task_facts_helpers import BASE_TIME, started_run

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def test_v3_start_atomically_freezes_queryable_policies(database_session_factory) -> None:
    policy = CoordinationPolicy(max_rounds=2, max_total_tasks=5)
    store, started = await started_run(
        database_session_factory,
        workflow_version=WorkflowVersion.V3,
        model_budget=loop_policy(),
        coordination_policy=policy,
    )

    persisted = await store.get_run(started.run.id)
    stored_policy = await CoordinationPlanQueries(database_session_factory).get_policy(
        started.run.id
    )

    assert persisted.workflow_version is WorkflowVersion.V3
    assert persisted.graph_version == "dynamic-investigation-graph-v13"
    assert persisted.coordination_policy_version == "dynamic-coordination-v1"
    assert persisted.coordination_policy == policy
    assert persisted.call_budget is not None
    assert persisted.model_budget == loop_policy()
    assert persisted.context_policy_version == "summary-context-v1"
    assert persisted.working_summary_policy_version == "task-working-summary-v1"
    assert persisted.evidence_policy_version == "task-evidence-v1"
    assert persisted.prompt_version == "summary-context-v1"
    assert persisted.observation_cutoff_at is None
    assert stored_policy is not None and stored_policy.policy == policy
    assert stored_policy.created_at == started.run.created_at


async def test_legacy_claim_skips_v3_and_can_claim_later_v2(database_session_factory) -> None:
    v3_store, v3 = await started_run(
        database_session_factory,
        workflow_version=WorkflowVersion.V3,
        model_budget=loop_policy(),
    )
    _, v2 = await started_run(
        database_session_factory,
        workflow_version=WorkflowVersion.V2,
    )

    claimed = await v3_store.claim_next(
        worker_id="legacy-worker",
        occurred_at=BASE_TIME + timedelta(seconds=2),
        lease_duration=timedelta(seconds=30),
    )
    v3_run = await v3_store.get_run(v3.run.id)
    v3_work = await v3_store.get_work_item(v3.work_item.id)

    assert claimed is not None and claimed.run.id == v2.run.id
    assert v3_run.status is InvestigationRunStatus.QUEUED
    assert v3_run.started_at is None
    assert v3_work.status is WorkItemStatus.PENDING
    assert v3_work.attempt_count == 0


async def test_explicit_v3_capability_receives_frozen_start_contract(
    database_session_factory,
) -> None:
    store, started = await started_run(
        database_session_factory,
        workflow_version=WorkflowVersion.V3,
        model_budget=loop_policy(),
    )

    claimed = await store.claim_next(
        worker_id="future-v3-worker",
        occurred_at=BASE_TIME + timedelta(seconds=2),
        lease_duration=timedelta(seconds=30),
        supported_workflow_versions=(WorkflowVersion.V3,),
    )

    assert claimed is not None
    assert claimed.run.id == started.run.id
    assert claimed.run.graph_version == "dynamic-investigation-graph-v13"
    assert claimed.run.coordination_policy == CoordinationPolicy()
