"""评测结果 PostgreSQL 持久化集成测试。"""

import asyncio
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opsswarm.evaluation import MVP_INVESTIGATION_DATASET_V1
from opsswarm.evaluation.errors import (
    EvaluationDatasetVersionConflict,
    EvaluationExperimentConflict,
    EvaluationIdempotencyConflict,
    EvaluationRepetitionConflict,
    EvaluationReportMismatch,
    EvaluationRunNotFound,
)
from opsswarm.evaluation.fixtures import run_fixture_benchmark
from opsswarm.evaluation.metadata import (
    EvaluationExperimentSpec,
    EvaluationFailureCounts,
    EvaluationKind,
    EvaluationModeTelemetry,
    EvaluationRunTelemetry,
    empty_evaluation_telemetry,
    fixture_experiment_spec,
)
from opsswarm.evaluation.persistence import EvaluationRunSubmission, EvaluationStore
from opsswarm.evaluation.persistence.tables import EvaluationCaseSetRow, EvaluationRunRow

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def _submission(
    idempotency_key: str,
    *,
    runner_name: str = "fixture-contract-v1",
    experiment_key: str | None = None,
    repetition_index: int = 1,
    telemetry: EvaluationRunTelemetry | None = None,
) -> EvaluationRunSubmission:
    report = await run_fixture_benchmark()
    return EvaluationRunSubmission(
        dataset=MVP_INVESTIGATION_DATASET_V1,
        report=report,
        idempotency_key=idempotency_key,
        runner_name=runner_name,
        experiment=fixture_experiment_spec(experiment_key or idempotency_key),
        repetition_index=repetition_index,
        telemetry=telemetry or empty_evaluation_telemetry(),
        created_at=report.generated_at,
    )


async def test_save_get_and_replay_report(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = EvaluationStore(database_session_factory)
    submission = await _submission("evaluation-save-001")

    created = await store.save_report(submission)
    replayed = await store.save_report(submission)
    loaded = await store.get_run(created.run.id)

    assert created.created is True
    assert replayed.created is False
    assert replayed.run.id == created.run.id
    assert loaded == created.run
    assert loaded.report == submission.report
    assert loaded.dataset_key == MVP_INVESTIGATION_DATASET_V1.dataset_key
    assert loaded.scenario_count == len(MVP_INVESTIGATION_DATASET_V1.scenarios)
    assert loaded.experiment.experiment_key == "evaluation-save-001"
    assert loaded.experiment.model_name is None
    assert loaded.repetition_index == 1
    assert loaded.telemetry == empty_evaluation_telemetry()


async def test_list_runs_uses_stable_descending_cursor(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = EvaluationStore(database_session_factory)
    first = await store.save_report(await _submission("evaluation-page-001"))
    second = await store.save_report(await _submission("evaluation-page-002"))

    first_page = await store.list_runs(limit=1)
    second_page = await store.list_runs(
        limit=1,
        before_sequence=first_page.next_before_sequence,
    )

    assert tuple(item.id for item in first_page.items) == (second.run.id,)
    assert first_page.next_before_sequence == second.run.sequence
    assert tuple(item.id for item in second_page.items) == (first.run.id,)
    assert second_page.next_before_sequence is None


async def test_same_idempotency_key_rejects_different_report(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = EvaluationStore(database_session_factory)
    submission = await _submission("evaluation-conflict-001")
    await store.save_report(submission)
    changed_report = submission.report.model_copy(
        update={"generated_at": submission.report.generated_at + timedelta(seconds=1)}
    )
    changed = submission.model_copy(
        update={
            "report": changed_report,
            "created_at": changed_report.generated_at,
        }
    )

    with pytest.raises(EvaluationIdempotencyConflict):
        await store.save_report(changed)


async def test_same_dataset_key_rejects_silent_content_change(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = EvaluationStore(database_session_factory)
    original = await _submission("evaluation-dataset-001")
    await store.save_report(original)
    changed_dataset = original.dataset.model_copy(
        update={"scoring_policy_version": "investigation-score-v1-modified"}
    )
    changed_report = original.report.model_copy(
        update={
            "dataset_content_hash": changed_dataset.content_hash(),
            "scoring_policy_version": changed_dataset.scoring_policy_version,
        }
    )
    changed = EvaluationRunSubmission(
        dataset=changed_dataset,
        report=changed_report,
        idempotency_key="evaluation-dataset-002",
        runner_name=original.runner_name,
        experiment=fixture_experiment_spec("evaluation-dataset-changed"),
        repetition_index=1,
        telemetry=empty_evaluation_telemetry(),
        created_at=original.created_at,
    )

    with pytest.raises(EvaluationDatasetVersionConflict):
        await store.save_report(changed)


async def test_concurrent_replay_creates_only_one_run(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = EvaluationStore(database_session_factory)
    submission = await _submission("evaluation-concurrent-001")

    outcomes = await asyncio.gather(
        store.save_report(submission),
        store.save_report(submission),
    )

    assert sum(item.created for item in outcomes) == 1
    assert len({item.run.id for item in outcomes}) == 1
    page = await store.list_runs()
    assert len(page.items) == 1


async def test_same_experiment_supports_ordered_repetitions(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = EvaluationStore(database_session_factory)
    first = await store.save_report(
        await _submission(
            "evaluation-repeat-001",
            experiment_key="evaluation-repeat-group",
            repetition_index=1,
        )
    )
    second = await store.save_report(
        await _submission(
            "evaluation-repeat-002",
            experiment_key="evaluation-repeat-group",
            repetition_index=2,
        )
    )

    assert first.run.experiment == second.run.experiment
    assert (first.run.repetition_index, second.run.repetition_index) == (1, 2)


async def test_repetition_slot_rejects_different_idempotency_key(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = EvaluationStore(database_session_factory)
    await store.save_report(
        await _submission(
            "evaluation-slot-001",
            experiment_key="evaluation-slot-group",
        )
    )

    with pytest.raises(EvaluationRepetitionConflict):
        await store.save_report(
            await _submission(
                "evaluation-slot-002",
                experiment_key="evaluation-slot-group",
            )
        )


async def test_experiment_key_rejects_changed_runner(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = EvaluationStore(database_session_factory)
    await store.save_report(
        await _submission(
            "evaluation-experiment-001",
            experiment_key="evaluation-experiment-group",
        )
    )

    with pytest.raises(EvaluationExperimentConflict):
        await store.save_report(
            await _submission(
                "evaluation-experiment-002",
                experiment_key="evaluation-experiment-group",
                repetition_index=2,
                runner_name="fixture-contract-v2",
            )
        )


async def test_idempotency_replay_rejects_changed_telemetry(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = EvaluationStore(database_session_factory)
    original = await _submission("evaluation-telemetry-001")
    await store.save_report(original)
    changed_telemetry = EvaluationRunTelemetry(
        single_agent=EvaluationModeTelemetry(
            input_tokens=10,
            output_tokens=5,
            total_tokens=15,
            estimated_cost_usd=Decimal("0.000125"),
        ),
        multi_agent=EvaluationModeTelemetry(),
    )

    with pytest.raises(EvaluationIdempotencyConflict):
        await store.save_report(original.model_copy(update={"telemetry": changed_telemetry}))


async def test_model_metadata_and_nonzero_telemetry_round_trip(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = EvaluationStore(database_session_factory)
    original = await _submission("evaluation-real-model-001")
    telemetry = EvaluationRunTelemetry(
        single_agent=EvaluationModeTelemetry(
            input_tokens=1200,
            output_tokens=300,
            total_tokens=1500,
            estimated_cost_usd=Decimal("0.004500"),
            failures=EvaluationFailureCounts(timeout=1),
        ),
        multi_agent=EvaluationModeTelemetry(
            input_tokens=2200,
            output_tokens=500,
            total_tokens=2700,
            estimated_cost_usd=Decimal("0.008100"),
        ),
    )
    submission = original.model_copy(
        update={
            "experiment": EvaluationExperimentSpec(
                experiment_key="evaluation-real-model-group",
                evaluation_kind=EvaluationKind.REAL_MODEL,
                model_provider="openai-compatible",
                model_name="example-model",
                model_parameters_hash="c" * 64,
                single_agent_prompt_version="single-agent-v2",
                multi_agent_prompt_version="multi-agent-parallel-v2",
            ),
            "telemetry": telemetry,
        }
    )

    created = await store.save_report(submission)
    loaded = await store.get_run(created.run.id)

    assert loaded.experiment.model_name == "example-model"
    assert loaded.telemetry == telemetry
    assert loaded.telemetry.single_agent.failures.timeout == 1


async def test_missing_run_raises_domain_error(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = EvaluationStore(database_session_factory)

    with pytest.raises(EvaluationRunNotFound):
        await store.get_run(uuid4())


async def test_read_rejects_tampered_dataset_snapshot(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = EvaluationStore(database_session_factory)
    await store.save_report(await _submission("evaluation-integrity-001"))
    async with database_session_factory() as session, session.begin():
        await session.execute(
            update(EvaluationCaseSetRow).values(dataset_snapshot={"tampered": True})
        )

    with pytest.raises(EvaluationReportMismatch):
        await store.list_runs()


async def test_read_rejects_tampered_telemetry(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = EvaluationStore(database_session_factory)
    creation = await store.save_report(await _submission("evaluation-telemetry-integrity-001"))
    async with database_session_factory() as session, session.begin():
        await session.execute(
            update(EvaluationRunRow)
            .where(EvaluationRunRow.id == creation.run.id)
            .values(
                single_agent_telemetry={
                    "input_tokens": 10,
                    "output_tokens": 5,
                    "total_tokens": 99,
                    "estimated_cost_usd": "0",
                    "failures": {
                        "model_request_error": 0,
                        "tool_error": 0,
                        "timeout": 0,
                        "structured_output_error": 0,
                        "safety_rejection": 0,
                    },
                }
            )
        )

    with pytest.raises(EvaluationReportMismatch):
        await store.get_run(creation.run.id)
