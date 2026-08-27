"""离线评测命令显式持久化开关测试。"""

import json

import pytest

from opsswarm.evaluation import MVP_INVESTIGATION_DATASET_V1
from opsswarm.evaluation.errors import EvaluationReportMismatch
from opsswarm.evaluation.fixtures import run_fixture_benchmark
from opsswarm.evaluation.main import EvaluationCommandOptions, async_main, parse_arguments
from opsswarm.evaluation.persistence import EvaluationRunSubmission


def test_default_command_does_not_enable_persistence() -> None:
    options = parse_arguments([])

    assert options == EvaluationCommandOptions(
        persist=False,
        idempotency_key=None,
        runner_name="fixture-contract-v1",
    )


def test_persist_requires_explicit_idempotency_key() -> None:
    with pytest.raises(SystemExit):
        parse_arguments(["--persist"])


def test_idempotency_key_cannot_be_used_without_persist() -> None:
    with pytest.raises(SystemExit):
        parse_arguments(["--idempotency-key", "evaluation-cli-001"])


def test_persist_options_are_parsed_together() -> None:
    options = parse_arguments(
        [
            "--persist",
            "--idempotency-key",
            "evaluation-cli-001",
            "--runner-name",
            "fixture-contract-v2",
        ]
    )

    assert options.persist is True
    assert options.idempotency_key == "evaluation-cli-001"
    assert options.runner_name == "fixture-contract-v2"


@pytest.mark.asyncio
async def test_default_execution_prints_report_without_database_write(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = await async_main(
        EvaluationCommandOptions(
            persist=False,
            idempotency_key=None,
            runner_name="fixture-contract-v1",
        )
    )

    output = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert output["dataset_key"] == "opsswarm-mvp-investigation-v1"
    assert len(output["scenario_results"]) == 3


@pytest.mark.asyncio
async def test_submission_rejects_aggregate_not_derived_from_scenarios() -> None:
    report = await run_fixture_benchmark()
    changed_report = report.model_copy(
        update={"single_agent": report.single_agent.model_copy(update={"top1_accuracy": 0.0})}
    )

    with pytest.raises(EvaluationReportMismatch):
        EvaluationRunSubmission(
            dataset=MVP_INVESTIGATION_DATASET_V1,
            report=changed_report,
            idempotency_key="evaluation-metrics-001",
            runner_name="fixture-contract-v1",
            created_at=changed_report.generated_at,
        )


@pytest.mark.asyncio
async def test_submission_rejects_metrics_not_derived_from_frozen_truth() -> None:
    report = await run_fixture_benchmark()
    first_result = report.scenario_results[0]
    changed_single = first_result.single_agent.model_copy(update={"root_cause": "undetermined"})
    changed_result = first_result.model_copy(update={"single_agent": changed_single})
    changed_report = report.model_copy(
        update={"scenario_results": (changed_result, *report.scenario_results[1:])}
    )

    with pytest.raises(EvaluationReportMismatch):
        EvaluationRunSubmission(
            dataset=MVP_INVESTIGATION_DATASET_V1,
            report=changed_report,
            idempotency_key="evaluation-truth-001",
            runner_name="fixture-contract-v1",
            created_at=changed_report.generated_at,
        )
