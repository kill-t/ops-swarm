"""真实 PostgreSQL 中评测尝试生命周期与幂等约束测试。"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opsswarm.evaluation import MVP_INVESTIGATION_DATASET_V1
from opsswarm.evaluation.attempts import (
    EvaluationAttemptBudget,
    EvaluationAttemptStatus,
    EvaluationAttemptSubmission,
    EvaluationFailureCategory,
)
from opsswarm.evaluation.errors import EvaluationAttemptAlreadyFinished
from opsswarm.evaluation.metadata import EvaluationFailureCounts, EvaluationModeTelemetry
from opsswarm.evaluation.models import InvestigationModeBenchmark
from opsswarm.evaluation.persistence import EvaluationStore
from opsswarm.evaluation.real_batch import RealModelBatchRunner
from opsswarm.evaluation.real_model import (
    ModelUsageBudgetHandler,
    RealModelSmokeRunner,
    real_model_experiment_spec,
)
from opsswarm.evaluation.runner import build_mode_benchmark
from opsswarm.investigation.models import (
    InvestigationConclusion,
    InvestigationMode,
    InvestigationRunResult,
)
from opsswarm.investigation.scenarios import RELEASE_500_SCENARIO, InvestigationScenario
from opsswarm.investigation.settings import LlmSettings

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

STARTED_AT = datetime(2026, 8, 28, 8, 0, tzinfo=UTC)


def _budget() -> EvaluationAttemptBudget:
    return EvaluationAttemptBudget(
        timeout_seconds=60,
        max_model_requests=6,
        max_tool_calls=3,
        max_output_tokens_per_request=512,
        max_estimated_cost_usd=Decimal("0.10"),
        input_cost_per_million_usd=Decimal("1"),
        output_cost_per_million_usd=Decimal("2"),
    )


def _submission(mode: InvestigationMode) -> EvaluationAttemptSubmission:
    settings = LlmSettings(api_key="test-only-key", model="test-model")
    return EvaluationAttemptSubmission(
        dataset=MVP_INVESTIGATION_DATASET_V1,
        experiment=real_model_experiment_spec(
            "real-model-store-exp01",
            llm_settings=settings,
            budget=_budget(),
        ),
        runner_name="real-model-smoke-v1",
        repetition_index=1,
        scenario_id=RELEASE_500_SCENARIO.scenario_id,
        mode=mode,
        idempotency_key=f"real-model-store-r01:{mode.value}",
        budget=_budget(),
        started_at=STARTED_AT,
    )


def _run_result() -> InvestigationRunResult:
    scenario = RELEASE_500_SCENARIO
    return InvestigationRunResult(
        conclusion=InvestigationConclusion(
            root_cause=scenario.expected_root_cause,
            confidence=0.95,
            summary="冻结测试结论。",
            evidence_ids=tuple(sorted(scenario.required_evidence_ids)),
            recommendation=scenario.expected_recommendation,
            requires_human_approval=True,
            safety_notes=("只允许审批后的白名单执行。",),
        ),
        returned_evidence_ids=tuple(item.evidence_id for item in scenario.evidence),
        tool_call_count=3,
    )


def _result(mode: InvestigationMode) -> InvestigationModeBenchmark:
    return build_mode_benchmark(mode, _run_result(), RELEASE_500_SCENARIO, 12.5)


class _SuccessfulSmokeRunner(RealModelSmokeRunner):
    """不访问网络，但经过真实冒烟编排与持久状态机。"""

    async def _investigate(
        self,
        mode: InvestigationMode,
        scenario: InvestigationScenario,
        usage: ModelUsageBudgetHandler,
    ) -> InvestigationRunResult:
        del mode, scenario, usage
        return _run_result()


class _SuccessfulBatchRunner(RealModelSmokeRunner):
    """为批量编排返回与当前冻结场景一致的离线成功结果。"""

    async def _investigate(
        self,
        mode: InvestigationMode,
        scenario: InvestigationScenario,
        usage: ModelUsageBudgetHandler,
    ) -> InvestigationRunResult:
        del mode, usage
        return InvestigationRunResult(
            conclusion=InvestigationConclusion(
                root_cause=scenario.expected_root_cause,
                confidence=0.95,
                summary="批量编排离线成功结论。",
                evidence_ids=tuple(sorted(scenario.required_evidence_ids)),
                recommendation=scenario.expected_recommendation,
                requires_human_approval=(
                    scenario.expected_recommendation.value == "rollback_release"
                ),
            ),
            returned_evidence_ids=tuple(item.evidence_id for item in scenario.evidence),
            tool_call_count=3,
        )


async def test_attempt_is_persisted_before_success_and_replay_is_idempotent(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = EvaluationStore(database_session_factory)
    submission = _submission(InvestigationMode.SINGLE_AGENT)

    started = await store.start_attempt(submission)
    replayed = await store.start_attempt(
        submission.model_copy(update={"started_at": STARTED_AT + timedelta(minutes=1)})
    )
    completed = await store.complete_attempt(
        started.attempt.id,
        result=_result(InvestigationMode.SINGLE_AGENT),
        telemetry=EvaluationModeTelemetry(model_request_count=2),
        finished_at=STARTED_AT + timedelta(seconds=2),
    )

    assert started.created is True
    assert started.attempt.status is EvaluationAttemptStatus.RUNNING
    assert replayed.created is False
    assert replayed.attempt.id == started.attempt.id
    assert completed.status is EvaluationAttemptStatus.SUCCEEDED
    assert completed.result is not None
    assert completed.telemetry.model_request_count == 2


async def test_failed_attempt_cannot_be_overwritten(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = EvaluationStore(database_session_factory)
    started = await store.start_attempt(_submission(InvestigationMode.MULTI_AGENT))
    failed = await store.fail_attempt(
        started.attempt.id,
        category=EvaluationFailureCategory.TIMEOUT,
        message="模型请求超过 60 秒",
        telemetry=EvaluationModeTelemetry(failures=EvaluationFailureCounts(timeout=1)),
        finished_at=STARTED_AT + timedelta(seconds=60),
    )

    assert failed.status is EvaluationAttemptStatus.FAILED
    assert failed.failure_category is EvaluationFailureCategory.TIMEOUT
    page = await store.list_attempts()
    assert page.items == (failed,)

    with pytest.raises(EvaluationAttemptAlreadyFinished):
        await store.complete_attempt(
            failed.id,
            result=_result(InvestigationMode.MULTI_AGENT),
            telemetry=EvaluationModeTelemetry(),
            finished_at=STARTED_AT + timedelta(seconds=61),
        )


async def test_smoke_runner_creates_two_modes_and_replay_does_not_rerun(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = EvaluationStore(database_session_factory)
    runner = _SuccessfulSmokeRunner(
        store=store,
        llm_settings=LlmSettings(api_key="test-only-key", model="test-model"),
        budget=_budget(),
        utc_clock=lambda: STARTED_AT,
        monotonic_clock=lambda: 0,
    )

    first = await runner.run(
        MVP_INVESTIGATION_DATASET_V1,
        scenario_id=RELEASE_500_SCENARIO.scenario_id,
        experiment_key="real-model-runner-exp01",
        repetition_index=1,
        idempotency_prefix="real-model-runner-r01",
    )
    replay = await runner.run(
        MVP_INVESTIGATION_DATASET_V1,
        scenario_id=RELEASE_500_SCENARIO.scenario_id,
        experiment_key="real-model-runner-exp01",
        repetition_index=1,
        idempotency_prefix="real-model-runner-r01",
    )

    assert tuple(item.status for item in first) == (
        EvaluationAttemptStatus.SUCCEEDED,
        EvaluationAttemptStatus.SUCCEEDED,
    )
    assert tuple(item.id for item in replay) == tuple(item.id for item in first)
    assert len((await store.list_attempts()).items) == 2


async def test_batch_runner_persists_complete_matrix_and_replays_without_duplicates(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = EvaluationStore(database_session_factory)
    budget = _budget()
    smoke_runner = _SuccessfulBatchRunner(
        store=store,
        llm_settings=LlmSettings(api_key="test-only-key", model="test-model"),
        budget=budget,
        utc_clock=lambda: STARTED_AT,
        monotonic_clock=lambda: 0,
    )
    batch_runner = RealModelBatchRunner(
        smoke_runner=smoke_runner,
        budget=budget,
        max_batch_cost_usd=Decimal("0.10"),
        utc_clock=lambda: STARTED_AT,
    )
    scenario_ids = tuple(item.scenario_id for item in MVP_INVESTIGATION_DATASET_V1.scenarios)

    first = await batch_runner.run(
        MVP_INVESTIGATION_DATASET_V1,
        experiment_key="real-model-batch-exp01",
        repetition_indexes=(1,),
        scenario_ids=scenario_ids,
        idempotency_prefix="real-model-batch-exp01",
    )
    replay = await batch_runner.run(
        MVP_INVESTIGATION_DATASET_V1,
        experiment_key="real-model-batch-exp01",
        repetition_indexes=(1,),
        scenario_ids=scenario_ids,
        idempotency_prefix="real-model-batch-exp01",
    )

    assert len(first.attempts) == 6
    assert first.single_agent.success_rate == 1
    assert first.multi_agent.success_rate == 1
    assert tuple(item.sequence for item in replay.attempts) == tuple(
        item.sequence for item in first.attempts
    )
    database_pair = tuple(item for item in first.attempts if item.scenario_id == scenario_ids[1])
    single = next(item for item in database_pair if item.mode is InvestigationMode.SINGLE_AGENT)
    multi = next(item for item in database_pair if item.mode is InvestigationMode.MULTI_AGENT)
    assert multi.sequence < single.sequence
    assert len((await store.list_attempts()).items) == 6
