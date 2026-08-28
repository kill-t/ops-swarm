"""评测集和公平对照报告的 PostgreSQL 原子持久化。"""

import hashlib
import json
from datetime import UTC, datetime
from typing import cast
from uuid import UUID, uuid4

from pydantic import ValidationError
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opsswarm.evaluation.attempts import (
    EvaluationAttemptBudget,
    EvaluationAttemptCreation,
    EvaluationAttemptPage,
    EvaluationAttemptStatus,
    EvaluationAttemptSubmission,
    EvaluationFailureCategory,
    StoredEvaluationAttempt,
)
from opsswarm.evaluation.errors import (
    EvaluationAttemptAlreadyFinished,
    EvaluationAttemptConflict,
    EvaluationAttemptNotFound,
    EvaluationDatasetVersionConflict,
    EvaluationExperimentConflict,
    EvaluationIdempotencyConflict,
    EvaluationRepetitionConflict,
    EvaluationReportMismatch,
    EvaluationRunNotFound,
)
from opsswarm.evaluation.metadata import (
    EvaluationExperimentSpec,
    EvaluationModeTelemetry,
    EvaluationRunTelemetry,
)
from opsswarm.evaluation.models import (
    InvestigationAggregateDelta,
    InvestigationAggregateMetrics,
    InvestigationBenchmarkReport,
    InvestigationEvaluationDataset,
    InvestigationModeBenchmark,
    InvestigationScenarioBenchmark,
)
from opsswarm.evaluation.persistence.records import (
    EvaluationRunCreation,
    EvaluationRunPage,
    EvaluationRunSubmission,
    StoredEvaluationRun,
    StoredEvaluationRunSummary,
)
from opsswarm.evaluation.persistence.tables import (
    EvaluationAttemptRow,
    EvaluationCaseSetRow,
    EvaluationExperimentRow,
    EvaluationResultRow,
    EvaluationRunRow,
)
from opsswarm.investigation.models import InvestigationMode


class EvaluationStore:
    """原子保存完整报告，并提供稳定游标的只读查询。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def save_report(self, submission: EvaluationRunSubmission) -> EvaluationRunCreation:
        """按幂等键保存一份完成报告，重放时不创建重复明细。"""

        report_hash = _report_content_hash(submission.report)
        created_at = max(submission.created_at, submission.report.generated_at)
        async with self._session_factory() as session, session.begin():
            case_set = await _ensure_case_set(
                session,
                dataset=submission.dataset,
                created_at=created_at,
            )
            experiment = await _ensure_experiment(
                session,
                case_set=case_set,
                spec=submission.experiment,
                runner_name=submission.runner_name,
                created_at=created_at,
            )
            run_id = uuid4()
            inserted_id = await session.scalar(
                insert(EvaluationRunRow)
                .values(
                    id=run_id,
                    evaluation_case_set_id=case_set.id,
                    experiment_id=experiment.id,
                    repetition_index=submission.repetition_index,
                    idempotency_key=submission.idempotency_key,
                    report_content_hash=report_hash,
                    runner_name=submission.runner_name,
                    generated_at=submission.report.generated_at,
                    single_agent_aggregate=submission.report.single_agent.model_dump(mode="json"),
                    multi_agent_aggregate=submission.report.multi_agent.model_dump(mode="json"),
                    aggregate_delta=submission.report.delta.model_dump(mode="json"),
                    single_agent_telemetry=submission.telemetry.single_agent.model_dump(
                        mode="json"
                    ),
                    multi_agent_telemetry=submission.telemetry.multi_agent.model_dump(mode="json"),
                    created_at=created_at,
                )
                .on_conflict_do_nothing()
                .returning(EvaluationRunRow.id)
            )
            if inserted_id is None:
                existing = await session.scalar(
                    select(EvaluationRunRow).where(
                        EvaluationRunRow.idempotency_key == submission.idempotency_key
                    )
                )
                if existing is not None:
                    if not _run_matches_submission(
                        existing,
                        case_set,
                        experiment,
                        report_hash,
                        submission,
                    ):
                        raise EvaluationIdempotencyConflict(submission.idempotency_key)
                    return EvaluationRunCreation(
                        run=await _load_run(session, existing.id),
                        created=False,
                    )
                occupied = await session.scalar(
                    select(EvaluationRunRow).where(
                        EvaluationRunRow.experiment_id == experiment.id,
                        EvaluationRunRow.repetition_index == submission.repetition_index,
                    )
                )
                if occupied is not None:
                    raise EvaluationRepetitionConflict(
                        submission.experiment.experiment_key,
                        submission.repetition_index,
                    )
                raise RuntimeError("评测运行插入冲突无法由幂等键或重复序号定位")

            session.add_all(
                [
                    EvaluationResultRow(
                        id=uuid4(),
                        evaluation_run_id=run_id,
                        scenario_id=result.scenario_id,
                        position=position,
                        comparison=result.model_dump(mode="json"),
                        created_at=created_at,
                    )
                    for position, result in enumerate(
                        submission.report.scenario_results,
                        start=1,
                    )
                ]
            )
            await session.flush()
            return EvaluationRunCreation(
                run=await _load_run(session, run_id),
                created=True,
            )

    async def start_attempt(
        self,
        submission: EvaluationAttemptSubmission,
    ) -> EvaluationAttemptCreation:
        """在外部模型调用前登记运行中尝试，并对重放保持幂等。"""

        async with self._session_factory() as session, session.begin():
            case_set = await _ensure_case_set(
                session,
                dataset=submission.dataset,
                created_at=submission.started_at,
            )
            experiment = await _ensure_experiment(
                session,
                case_set=case_set,
                spec=submission.experiment,
                runner_name=submission.runner_name,
                created_at=submission.started_at,
            )
            attempt_id = uuid4()
            inserted_id = await session.scalar(
                insert(EvaluationAttemptRow)
                .values(
                    id=attempt_id,
                    evaluation_case_set_id=case_set.id,
                    experiment_id=experiment.id,
                    repetition_index=submission.repetition_index,
                    scenario_id=submission.scenario_id,
                    mode=submission.mode.value,
                    idempotency_key=submission.idempotency_key,
                    status=EvaluationAttemptStatus.RUNNING.value,
                    budget_snapshot=submission.budget.model_dump(mode="json"),
                    telemetry=EvaluationModeTelemetry().model_dump(mode="json"),
                    result_snapshot=None,
                    failure_category=None,
                    failure_message=None,
                    started_at=submission.started_at,
                    finished_at=None,
                    created_at=submission.started_at,
                )
                .on_conflict_do_nothing()
                .returning(EvaluationAttemptRow.id)
            )
            if inserted_id is not None:
                return EvaluationAttemptCreation(
                    attempt=await _load_attempt(session, inserted_id),
                    created=True,
                )

            existing = await session.scalar(
                select(EvaluationAttemptRow).where(
                    EvaluationAttemptRow.idempotency_key == submission.idempotency_key
                )
            )
            if existing is not None:
                if not _attempt_matches_submission(existing, case_set, experiment, submission):
                    raise EvaluationAttemptConflict(submission.idempotency_key)
                return EvaluationAttemptCreation(
                    attempt=await _load_attempt(session, existing.id),
                    created=False,
                )
            raise EvaluationAttemptConflict(
                f"{submission.experiment.experiment_key}#"
                f"{submission.repetition_index}:{submission.scenario_id}:{submission.mode.value}"
            )

    async def complete_attempt(
        self,
        attempt_id: UUID,
        *,
        result: InvestigationModeBenchmark,
        telemetry: EvaluationModeTelemetry,
        finished_at: datetime,
    ) -> StoredEvaluationAttempt:
        """把运行中尝试一次性收敛为成功，终态不可覆盖。"""

        normalized_finished_at = _require_aware_utc(finished_at)
        async with self._session_factory() as session, session.begin():
            row = await _running_attempt_row(session, attempt_id)
            if row.mode != result.mode.value:
                raise ValueError("尝试模式与成功结果模式不一致")
            if normalized_finished_at < row.started_at:
                raise ValueError("评测尝试完成时间不能早于开始时间")
            budget = EvaluationAttemptBudget.model_validate(row.budget_snapshot)
            if (
                telemetry.model_request_count > budget.max_model_requests
                or telemetry.output_tokens
                > telemetry.model_request_count * budget.max_output_tokens_per_request
                or telemetry.estimated_cost_usd > budget.max_estimated_cost_usd
                or result.metrics.tool_call_count > budget.max_tool_calls
            ):
                raise ValueError("成功尝试遥测或工具次数超过冻结预算")
            updated_id = await session.scalar(
                update(EvaluationAttemptRow)
                .where(
                    EvaluationAttemptRow.id == attempt_id,
                    EvaluationAttemptRow.status == EvaluationAttemptStatus.RUNNING.value,
                )
                .values(
                    status=EvaluationAttemptStatus.SUCCEEDED.value,
                    telemetry=telemetry.model_dump(mode="json"),
                    result_snapshot=result.model_dump(mode="json"),
                    finished_at=normalized_finished_at,
                )
                .returning(EvaluationAttemptRow.id)
            )
            if updated_id is None:
                await _raise_attempt_terminal_error(session, attempt_id)
            return await _load_attempt(session, cast(UUID, updated_id))

    async def fail_attempt(
        self,
        attempt_id: UUID,
        *,
        category: EvaluationFailureCategory,
        message: str,
        telemetry: EvaluationModeTelemetry,
        finished_at: datetime,
    ) -> StoredEvaluationAttempt:
        """把运行中尝试一次性收敛为失败，并保存经过脱敏的有限错误。"""

        normalized_finished_at = _require_aware_utc(finished_at)
        normalized_message = message.strip()
        if not normalized_message or len(normalized_message) > 500:
            raise ValueError("failure_message 必须为 1 到 500 个字符")
        if getattr(telemetry.failures, category.value) < 1:
            raise ValueError("失败遥测必须包含对应失败分类计数")
        async with self._session_factory() as session, session.begin():
            row = await _running_attempt_row(session, attempt_id)
            if normalized_finished_at < row.started_at:
                raise ValueError("评测尝试完成时间不能早于开始时间")
            updated_id = await session.scalar(
                update(EvaluationAttemptRow)
                .where(
                    EvaluationAttemptRow.id == attempt_id,
                    EvaluationAttemptRow.status == EvaluationAttemptStatus.RUNNING.value,
                )
                .values(
                    status=EvaluationAttemptStatus.FAILED.value,
                    telemetry=telemetry.model_dump(mode="json"),
                    failure_category=category.value,
                    failure_message=normalized_message,
                    finished_at=normalized_finished_at,
                )
                .returning(EvaluationAttemptRow.id)
            )
            if updated_id is None:
                await _raise_attempt_terminal_error(session, attempt_id)
            return await _load_attempt(session, cast(UUID, updated_id))

    async def get_attempt(self, attempt_id: UUID) -> StoredEvaluationAttempt:
        """读取一条真实模型评测尝试。"""

        async with self._session_factory() as session:
            return await _load_attempt(session, attempt_id)

    async def list_attempts(
        self,
        *,
        limit: int = 50,
        before_sequence: int | None = None,
    ) -> EvaluationAttemptPage:
        """按不可变序号倒序分页读取成功、失败和中断尝试。"""

        if not 1 <= limit <= 100:
            raise ValueError("limit 必须在 1 到 100 之间")
        if before_sequence is not None and before_sequence < 1:
            raise ValueError("before_sequence 必须大于等于 1")
        statement = (
            select(EvaluationAttemptRow, EvaluationCaseSetRow, EvaluationExperimentRow)
            .join(
                EvaluationCaseSetRow,
                EvaluationCaseSetRow.id == EvaluationAttemptRow.evaluation_case_set_id,
            )
            .join(
                EvaluationExperimentRow,
                EvaluationExperimentRow.id == EvaluationAttemptRow.experiment_id,
            )
            .order_by(EvaluationAttemptRow.sequence.desc())
            .limit(limit + 1)
        )
        if before_sequence is not None:
            statement = statement.where(EvaluationAttemptRow.sequence < before_sequence)
        async with self._session_factory() as session:
            rows = (await session.execute(statement)).all()
        items = tuple(
            _stored_attempt(row, case_set, experiment) for row, case_set, experiment in rows[:limit]
        )
        return EvaluationAttemptPage(
            items=items,
            next_before_sequence=items[-1].sequence if len(rows) > limit else None,
        )

    async def get_run(self, run_id: UUID) -> StoredEvaluationRun:
        """读取一轮完整报告，并重新验证持久化内容哈希。"""

        async with self._session_factory() as session:
            return await _load_run(session, run_id)

    async def list_runs(
        self,
        *,
        limit: int = 50,
        before_sequence: int | None = None,
    ) -> EvaluationRunPage:
        """按不可变全局序号倒序分页，避免新增数据造成翻页漂移。"""

        if not 1 <= limit <= 100:
            raise ValueError("limit 必须在 1 到 100 之间")
        if before_sequence is not None and before_sequence < 1:
            raise ValueError("before_sequence 必须大于等于 1")

        statement = (
            select(EvaluationRunRow, EvaluationCaseSetRow, EvaluationExperimentRow)
            .join(
                EvaluationCaseSetRow,
                EvaluationCaseSetRow.id == EvaluationRunRow.evaluation_case_set_id,
            )
            .join(
                EvaluationExperimentRow,
                EvaluationExperimentRow.id == EvaluationRunRow.experiment_id,
            )
            .order_by(EvaluationRunRow.sequence.desc())
            .limit(limit + 1)
        )
        if before_sequence is not None:
            statement = statement.where(EvaluationRunRow.sequence < before_sequence)

        async with self._session_factory() as session:
            rows = (await session.execute(statement)).all()

        visible_rows = rows[:limit]
        items = tuple(
            _stored_summary(run, case_set, experiment) for run, case_set, experiment in visible_rows
        )
        next_cursor = items[-1].sequence if len(rows) > limit else None
        return EvaluationRunPage(items=items, next_before_sequence=next_cursor)


async def _ensure_case_set(
    session: AsyncSession,
    *,
    dataset: InvestigationEvaluationDataset,
    created_at: datetime,
) -> EvaluationCaseSetRow:
    dataset_hash = dataset.content_hash()
    snapshot = dataset.canonical_payload()
    case_set_id = uuid4()
    inserted_id = await session.scalar(
        insert(EvaluationCaseSetRow)
        .values(
            id=case_set_id,
            dataset_key=dataset.dataset_key,
            dataset_content_hash=dataset_hash,
            scoring_policy_version=dataset.scoring_policy_version,
            scenario_count=len(dataset.scenarios),
            dataset_snapshot=snapshot,
            created_at=created_at,
        )
        .on_conflict_do_nothing()
        .returning(EvaluationCaseSetRow.id)
    )
    if inserted_id is not None:
        row = await session.get(EvaluationCaseSetRow, inserted_id)
        if row is None:
            raise RuntimeError("新建评测集快照后无法读取")
        return row

    row = cast(
        EvaluationCaseSetRow | None,
        await session.scalar(
            select(EvaluationCaseSetRow).where(
                EvaluationCaseSetRow.dataset_key == dataset.dataset_key
            )
        ),
    )
    if row is None:
        raise EvaluationDatasetVersionConflict(dataset.dataset_key)
    if (
        row.dataset_content_hash != dataset_hash
        or row.scoring_policy_version != dataset.scoring_policy_version
        or row.scenario_count != len(dataset.scenarios)
        or row.dataset_snapshot != snapshot
    ):
        raise EvaluationDatasetVersionConflict(dataset.dataset_key)
    return row


async def _ensure_experiment(
    session: AsyncSession,
    *,
    case_set: EvaluationCaseSetRow,
    spec: EvaluationExperimentSpec,
    runner_name: str,
    created_at: datetime,
) -> EvaluationExperimentRow:
    """并发创建或校验一组不可静默改写的重复实验配置。"""

    experiment_id = uuid4()
    inserted_id = await session.scalar(
        insert(EvaluationExperimentRow)
        .values(
            id=experiment_id,
            experiment_key=spec.experiment_key,
            evaluation_case_set_id=case_set.id,
            evaluation_kind=spec.evaluation_kind.value,
            runner_name=runner_name,
            model_provider=spec.model_provider,
            model_name=spec.model_name,
            model_parameters_hash=spec.model_parameters_hash,
            single_agent_prompt_version=spec.single_agent_prompt_version,
            multi_agent_prompt_version=spec.multi_agent_prompt_version,
            created_at=created_at,
        )
        .on_conflict_do_nothing()
        .returning(EvaluationExperimentRow.id)
    )
    if inserted_id is not None:
        row = await session.get(EvaluationExperimentRow, inserted_id)
        if row is None:
            raise RuntimeError("新建评测实验后无法读取")
        return row

    row = cast(
        EvaluationExperimentRow | None,
        await session.scalar(
            select(EvaluationExperimentRow).where(
                EvaluationExperimentRow.experiment_key == spec.experiment_key
            )
        ),
    )
    if row is None or not _experiment_matches(row, case_set, spec, runner_name):
        raise EvaluationExperimentConflict(spec.experiment_key)
    return row


async def _load_attempt(session: AsyncSession, attempt_id: UUID) -> StoredEvaluationAttempt:
    joined = (
        await session.execute(
            select(EvaluationAttemptRow, EvaluationCaseSetRow, EvaluationExperimentRow)
            .join(
                EvaluationCaseSetRow,
                EvaluationCaseSetRow.id == EvaluationAttemptRow.evaluation_case_set_id,
            )
            .join(
                EvaluationExperimentRow,
                EvaluationExperimentRow.id == EvaluationAttemptRow.experiment_id,
            )
            .where(EvaluationAttemptRow.id == attempt_id)
        )
    ).one_or_none()
    if joined is None:
        raise EvaluationAttemptNotFound(attempt_id)
    return _stored_attempt(*joined)


def _stored_attempt(
    row: EvaluationAttemptRow,
    case_set: EvaluationCaseSetRow,
    experiment: EvaluationExperimentRow,
) -> StoredEvaluationAttempt:
    """从关联行恢复强类型尝试，并重新校验数据集与实验绑定。"""

    try:
        _validated_dataset(case_set)
        if row.evaluation_case_set_id != case_set.id or row.experiment_id != experiment.id:
            raise EvaluationReportMismatch("评测尝试、实验与数据集关联不一致")
        spec = EvaluationExperimentSpec.model_validate(
            {
                "experiment_key": experiment.experiment_key,
                "evaluation_kind": experiment.evaluation_kind,
                "model_provider": experiment.model_provider,
                "model_name": experiment.model_name,
                "model_parameters_hash": experiment.model_parameters_hash,
                "single_agent_prompt_version": experiment.single_agent_prompt_version,
                "multi_agent_prompt_version": experiment.multi_agent_prompt_version,
            }
        )
        result = (
            InvestigationModeBenchmark.model_validate(row.result_snapshot)
            if row.result_snapshot is not None
            else None
        )
        return StoredEvaluationAttempt(
            id=row.id,
            sequence=row.sequence,
            dataset_key=case_set.dataset_key,
            dataset_content_hash=case_set.dataset_content_hash,
            experiment=spec,
            runner_name=experiment.runner_name,
            repetition_index=row.repetition_index,
            scenario_id=row.scenario_id,
            mode=InvestigationMode(row.mode),
            idempotency_key=row.idempotency_key,
            status=EvaluationAttemptStatus(row.status),
            budget=EvaluationAttemptBudget.model_validate(row.budget_snapshot),
            telemetry=EvaluationModeTelemetry.model_validate(row.telemetry),
            result=result,
            failure_category=(
                EvaluationFailureCategory(row.failure_category)
                if row.failure_category is not None
                else None
            ),
            failure_message=row.failure_message,
            started_at=row.started_at,
            finished_at=row.finished_at,
            created_at=row.created_at,
        )
    except ValidationError as error:
        raise EvaluationReportMismatch("持久评测尝试无法通过完整性校验") from error


async def _raise_attempt_terminal_error(session: AsyncSession, attempt_id: UUID) -> None:
    row = await session.get(EvaluationAttemptRow, attempt_id)
    if row is None:
        raise EvaluationAttemptNotFound(attempt_id)
    raise EvaluationAttemptAlreadyFinished(attempt_id)


async def _running_attempt_row(
    session: AsyncSession,
    attempt_id: UUID,
) -> EvaluationAttemptRow:
    row = await session.get(EvaluationAttemptRow, attempt_id)
    if row is None:
        raise EvaluationAttemptNotFound(attempt_id)
    if row.status != EvaluationAttemptStatus.RUNNING.value:
        raise EvaluationAttemptAlreadyFinished(attempt_id)
    return row


def _attempt_matches_submission(
    row: EvaluationAttemptRow,
    case_set: EvaluationCaseSetRow,
    experiment: EvaluationExperimentRow,
    submission: EvaluationAttemptSubmission,
) -> bool:
    return (
        row.evaluation_case_set_id == case_set.id
        and row.experiment_id == experiment.id
        and row.repetition_index == submission.repetition_index
        and row.scenario_id == submission.scenario_id
        and row.mode == submission.mode.value
        and row.budget_snapshot == submission.budget.model_dump(mode="json")
    )


def _require_aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("评测尝试完成时间必须包含时区")
    return value.astimezone(UTC)


async def _load_run(session: AsyncSession, run_id: UUID) -> StoredEvaluationRun:
    joined = (
        await session.execute(
            select(EvaluationRunRow, EvaluationCaseSetRow, EvaluationExperimentRow)
            .join(
                EvaluationCaseSetRow,
                EvaluationCaseSetRow.id == EvaluationRunRow.evaluation_case_set_id,
            )
            .join(
                EvaluationExperimentRow,
                EvaluationExperimentRow.id == EvaluationRunRow.experiment_id,
            )
            .where(EvaluationRunRow.id == run_id)
        )
    ).one_or_none()
    if joined is None:
        raise EvaluationRunNotFound(run_id)
    run_row, case_set, experiment_row = joined
    result_rows = (
        await session.scalars(
            select(EvaluationResultRow)
            .where(EvaluationResultRow.evaluation_run_id == run_id)
            .order_by(EvaluationResultRow.position)
        )
    ).all()

    expected_positions = tuple(range(1, case_set.scenario_count + 1))
    if tuple(row.position for row in result_rows) != expected_positions:
        raise EvaluationReportMismatch("评测场景明细数量或顺序已损坏")
    try:
        dataset = _validated_dataset(case_set)
        scenario_results = tuple(
            InvestigationScenarioBenchmark.model_validate(row.comparison) for row in result_rows
        )
        report = InvestigationBenchmarkReport(
            dataset_key=case_set.dataset_key,
            dataset_content_hash=case_set.dataset_content_hash,
            scoring_policy_version=case_set.scoring_policy_version,
            generated_at=run_row.generated_at,
            scenario_results=scenario_results,
            single_agent=InvestigationAggregateMetrics.model_validate(
                run_row.single_agent_aggregate
            ),
            multi_agent=InvestigationAggregateMetrics.model_validate(run_row.multi_agent_aggregate),
            delta=InvestigationAggregateDelta.model_validate(run_row.aggregate_delta),
        )
        EvaluationRunSubmission(
            dataset=dataset,
            report=report,
            idempotency_key=run_row.idempotency_key,
            runner_name=run_row.runner_name,
            experiment=_validated_experiment(experiment_row, case_set, run_row),
            repetition_index=run_row.repetition_index,
            telemetry=_validated_telemetry(run_row),
            created_at=run_row.created_at,
        )
    except (ValidationError, EvaluationReportMismatch) as error:
        raise EvaluationReportMismatch("持久评测报告无法通过完整性校验") from error
    if any(
        row.scenario_id != result.scenario_id
        for row, result in zip(result_rows, scenario_results, strict=True)
    ):
        raise EvaluationReportMismatch("评测场景标识与明细内容不一致")
    if _report_content_hash(report) != run_row.report_content_hash:
        raise EvaluationReportMismatch("持久评测报告内容哈希不一致")

    summary = _stored_summary(run_row, case_set, experiment_row)
    return StoredEvaluationRun(
        **summary.model_dump(),
        idempotency_key=run_row.idempotency_key,
        report_content_hash=run_row.report_content_hash,
        report=report,
    )


def _stored_summary(
    run_row: EvaluationRunRow,
    case_set: EvaluationCaseSetRow,
    experiment_row: EvaluationExperimentRow,
) -> StoredEvaluationRunSummary:
    try:
        _validated_dataset(case_set)
        return StoredEvaluationRunSummary(
            id=run_row.id,
            sequence=run_row.sequence,
            dataset_key=case_set.dataset_key,
            dataset_content_hash=case_set.dataset_content_hash,
            scoring_policy_version=case_set.scoring_policy_version,
            scenario_count=case_set.scenario_count,
            runner_name=run_row.runner_name,
            experiment=_validated_experiment(experiment_row, case_set, run_row),
            repetition_index=run_row.repetition_index,
            telemetry=_validated_telemetry(run_row),
            generated_at=run_row.generated_at,
            created_at=run_row.created_at,
            single_agent=InvestigationAggregateMetrics.model_validate(
                run_row.single_agent_aggregate
            ),
            multi_agent=InvestigationAggregateMetrics.model_validate(run_row.multi_agent_aggregate),
            delta=InvestigationAggregateDelta.model_validate(run_row.aggregate_delta),
        )
    except ValidationError as error:
        raise EvaluationReportMismatch("评测运行聚合指标无法通过完整性校验") from error


def _validated_experiment(
    row: EvaluationExperimentRow,
    case_set: EvaluationCaseSetRow,
    run_row: EvaluationRunRow,
) -> EvaluationExperimentSpec:
    if row.evaluation_case_set_id != case_set.id or run_row.experiment_id != row.id:
        raise EvaluationReportMismatch("评测运行、实验与数据集关联不一致")
    if row.runner_name != run_row.runner_name:
        raise EvaluationReportMismatch("评测实验与运行器版本不一致")
    return EvaluationExperimentSpec.model_validate(
        {
            "experiment_key": row.experiment_key,
            "evaluation_kind": row.evaluation_kind,
            "model_provider": row.model_provider,
            "model_name": row.model_name,
            "model_parameters_hash": row.model_parameters_hash,
            "single_agent_prompt_version": row.single_agent_prompt_version,
            "multi_agent_prompt_version": row.multi_agent_prompt_version,
        }
    )


def _validated_telemetry(run_row: EvaluationRunRow) -> EvaluationRunTelemetry:
    return EvaluationRunTelemetry(
        single_agent=EvaluationModeTelemetry.model_validate(run_row.single_agent_telemetry),
        multi_agent=EvaluationModeTelemetry.model_validate(run_row.multi_agent_telemetry),
    )


def _experiment_matches_submission(
    row: EvaluationExperimentRow,
    case_set: EvaluationCaseSetRow,
    submission: EvaluationRunSubmission,
) -> bool:
    return _experiment_matches(row, case_set, submission.experiment, submission.runner_name)


def _experiment_matches(
    row: EvaluationExperimentRow,
    case_set: EvaluationCaseSetRow,
    spec: EvaluationExperimentSpec,
    runner_name: str,
) -> bool:
    return (
        row.evaluation_case_set_id == case_set.id
        and row.evaluation_kind == spec.evaluation_kind.value
        and row.runner_name == runner_name
        and row.model_provider == spec.model_provider
        and row.model_name == spec.model_name
        and row.model_parameters_hash == spec.model_parameters_hash
        and row.single_agent_prompt_version == spec.single_agent_prompt_version
        and row.multi_agent_prompt_version == spec.multi_agent_prompt_version
    )


def _run_matches_submission(
    row: EvaluationRunRow,
    case_set: EvaluationCaseSetRow,
    experiment: EvaluationExperimentRow,
    report_hash: str,
    submission: EvaluationRunSubmission,
) -> bool:
    return (
        row.evaluation_case_set_id == case_set.id
        and row.experiment_id == experiment.id
        and row.repetition_index == submission.repetition_index
        and row.report_content_hash == report_hash
        and row.runner_name == submission.runner_name
        and _validated_telemetry(row) == submission.telemetry
    )


def _validated_dataset(case_set: EvaluationCaseSetRow) -> InvestigationEvaluationDataset:
    try:
        dataset = InvestigationEvaluationDataset.model_validate(case_set.dataset_snapshot)
    except ValidationError as error:
        raise EvaluationReportMismatch("评测集快照无法通过结构校验") from error
    if (
        dataset.dataset_key != case_set.dataset_key
        or dataset.content_hash() != case_set.dataset_content_hash
        or dataset.scoring_policy_version != case_set.scoring_policy_version
        or len(dataset.scenarios) != case_set.scenario_count
        or dataset.canonical_payload() != case_set.dataset_snapshot
    ):
        raise EvaluationReportMismatch("评测集元数据与冻结快照不一致")
    return dataset


def _report_content_hash(report: InvestigationBenchmarkReport) -> str:
    payload = json.dumps(
        report.model_dump(mode="json"),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
