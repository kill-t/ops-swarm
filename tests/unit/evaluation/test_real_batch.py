"""真实模型批量报告的可靠性、成本和波动统计测试。"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import SecretStr

from opsswarm.evaluation import MVP_INVESTIGATION_DATASET_V1
from opsswarm.evaluation.attempts import (
    EvaluationAttemptBudget,
    EvaluationAttemptStatus,
    EvaluationFailureCategory,
    StoredEvaluationAttempt,
)
from opsswarm.evaluation.metadata import EvaluationFailureCounts, EvaluationModeTelemetry
from opsswarm.evaluation.real_batch import build_real_model_batch_report
from opsswarm.evaluation.real_model import real_model_experiment_spec
from opsswarm.evaluation.runner import build_mode_benchmark
from opsswarm.investigation.models import (
    InvestigationConclusion,
    InvestigationMode,
    InvestigationRunResult,
)
from opsswarm.investigation.scenarios import RELEASE_500_SCENARIO
from opsswarm.investigation.settings import LlmSettings

GENERATED_AT = datetime(2026, 8, 28, 6, tzinfo=UTC)


def _budget() -> EvaluationAttemptBudget:
    return EvaluationAttemptBudget(
        timeout_seconds=60,
        max_model_requests=6,
        max_tool_calls=3,
        max_output_tokens_per_request=512,
        max_estimated_cost_usd=Decimal("0.10"),
        input_cost_per_million_usd=Decimal("0.45"),
        output_cost_per_million_usd=Decimal("1.35"),
    )


def _attempt(
    *,
    sequence: int,
    repetition_index: int,
    mode: InvestigationMode,
    status: EvaluationAttemptStatus,
    cost: str,
    duration_ms: float,
) -> StoredEvaluationAttempt:
    started_at = GENERATED_AT + timedelta(seconds=sequence)
    succeeded = status is EvaluationAttemptStatus.SUCCEEDED
    scenario = RELEASE_500_SCENARIO
    result = None
    if succeeded:
        investigation = InvestigationRunResult(
            conclusion=InvestigationConclusion(
                root_cause=scenario.expected_root_cause,
                confidence=0.95,
                summary="批量统计测试结论。",
                evidence_ids=tuple(sorted(scenario.required_evidence_ids)),
                recommendation=scenario.expected_recommendation,
                requires_human_approval=True,
            ),
            returned_evidence_ids=tuple(item.evidence_id for item in scenario.evidence),
            tool_call_count=3,
        )
        result = build_mode_benchmark(mode, investigation, scenario, duration_ms)
    failures = EvaluationFailureCounts(
        timeout=0 if succeeded else 1,
    )
    return StoredEvaluationAttempt(
        id=uuid4(),
        sequence=sequence,
        dataset_key=MVP_INVESTIGATION_DATASET_V1.dataset_key,
        dataset_content_hash=MVP_INVESTIGATION_DATASET_V1.content_hash(),
        experiment=real_model_experiment_spec(
            "real-batch-unit-exp01",
            llm_settings=LlmSettings(api_key=SecretStr("test-only"), model="test-model"),
            budget=_budget(),
        ),
        runner_name="real-model-smoke-v1",
        repetition_index=repetition_index,
        scenario_id=scenario.scenario_id,
        mode=mode,
        idempotency_key=f"real-batch-unit-r{repetition_index}:{mode.value}",
        status=status,
        budget=_budget(),
        telemetry=EvaluationModeTelemetry(
            model_request_count=2 if mode is InvestigationMode.SINGLE_AGENT else 4,
            input_tokens=1000,
            output_tokens=200,
            total_tokens=1200,
            estimated_cost_usd=Decimal(cost),
            failures=failures,
        ),
        result=result,
        failure_category=None if succeeded else EvaluationFailureCategory.TIMEOUT,
        failure_message=None if succeeded else "模型请求超时",
        started_at=started_at,
        finished_at=started_at + timedelta(milliseconds=duration_ms),
        created_at=started_at,
    )


def test_batch_report_counts_failures_as_zero_and_keeps_success_quality() -> None:
    attempts = (
        _attempt(
            sequence=1,
            repetition_index=1,
            mode=InvestigationMode.SINGLE_AGENT,
            status=EvaluationAttemptStatus.SUCCEEDED,
            cost="0.001000",
            duration_ms=100,
        ),
        _attempt(
            sequence=2,
            repetition_index=1,
            mode=InvestigationMode.MULTI_AGENT,
            status=EvaluationAttemptStatus.SUCCEEDED,
            cost="0.002000",
            duration_ms=300,
        ),
        _attempt(
            sequence=3,
            repetition_index=2,
            mode=InvestigationMode.SINGLE_AGENT,
            status=EvaluationAttemptStatus.FAILED,
            cost="0.000500",
            duration_ms=300,
        ),
        _attempt(
            sequence=4,
            repetition_index=2,
            mode=InvestigationMode.MULTI_AGENT,
            status=EvaluationAttemptStatus.SUCCEEDED,
            cost="0.004000",
            duration_ms=500,
        ),
    )

    report = build_real_model_batch_report(
        MVP_INVESTIGATION_DATASET_V1,
        experiment_key="real-batch-unit-exp01",
        repetition_indexes=(1, 2),
        scenario_ids=(RELEASE_500_SCENARIO.scenario_id,),
        attempts=attempts,
        generated_at=GENERATED_AT,
    )

    assert report.single_agent.success_rate == 0.5
    assert report.single_agent.top1_accuracy_including_failures == 0.5
    assert report.single_agent.top1_accuracy_on_success == 1
    assert report.single_agent.failures.timeout == 1
    assert report.single_agent.mean_estimated_cost_usd == Decimal("0.000750")
    assert report.single_agent.estimated_cost_stddev_usd == Decimal("0.000250")
    assert report.single_agent.mean_duration_ms == 200
    assert report.single_agent.duration_stddev_ms == 100
    assert report.multi_agent.success_rate == 1
    assert report.multi_agent.estimated_cost_stddev_usd == Decimal("0.001000")
    assert report.delta.success_rate == 0.5
    assert report.delta.top1_accuracy_including_failures == 0.5
    assert report.delta.mean_estimated_cost_usd == Decimal("0.002250")


def test_batch_report_rejects_running_attempt() -> None:
    running = _attempt(
        sequence=1,
        repetition_index=1,
        mode=InvestigationMode.SINGLE_AGENT,
        status=EvaluationAttemptStatus.SUCCEEDED,
        cost="0.001000",
        duration_ms=100,
    ).model_copy(
        update={
            "status": EvaluationAttemptStatus.RUNNING,
            "result": None,
            "finished_at": None,
        }
    )

    with pytest.raises(ValueError, match="仍在运行"):
        build_real_model_batch_report(
            MVP_INVESTIGATION_DATASET_V1,
            experiment_key="real-batch-unit-exp01",
            repetition_indexes=(1,),
            scenario_ids=(RELEASE_500_SCENARIO.scenario_id,),
            attempts=(running,),
            generated_at=GENERATED_AT,
        )
