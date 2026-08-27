"""评测集和公平对照报告的 PostgreSQL 原子持久化。"""

import hashlib
import json
from typing import cast
from uuid import UUID, uuid4

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opsswarm.evaluation.errors import (
    EvaluationDatasetVersionConflict,
    EvaluationIdempotencyConflict,
    EvaluationReportMismatch,
    EvaluationRunNotFound,
)
from opsswarm.evaluation.models import (
    InvestigationAggregateDelta,
    InvestigationAggregateMetrics,
    InvestigationBenchmarkReport,
    InvestigationEvaluationDataset,
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
    EvaluationCaseSetRow,
    EvaluationResultRow,
    EvaluationRunRow,
)


class EvaluationStore:
    """原子保存完整报告，并提供稳定游标的只读查询。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def save_report(self, submission: EvaluationRunSubmission) -> EvaluationRunCreation:
        """按幂等键保存一份完成报告，重放时不创建重复明细。"""

        report_hash = _report_content_hash(submission.report)
        async with self._session_factory() as session, session.begin():
            case_set = await _ensure_case_set(session, submission)
            run_id = uuid4()
            created_at = max(submission.created_at, submission.report.generated_at)
            inserted_id = await session.scalar(
                insert(EvaluationRunRow)
                .values(
                    id=run_id,
                    evaluation_case_set_id=case_set.id,
                    idempotency_key=submission.idempotency_key,
                    report_content_hash=report_hash,
                    runner_name=submission.runner_name,
                    generated_at=submission.report.generated_at,
                    single_agent_aggregate=submission.report.single_agent.model_dump(mode="json"),
                    multi_agent_aggregate=submission.report.multi_agent.model_dump(mode="json"),
                    aggregate_delta=submission.report.delta.model_dump(mode="json"),
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
                if existing is None or existing.report_content_hash != report_hash:
                    raise EvaluationIdempotencyConflict(submission.idempotency_key)
                return EvaluationRunCreation(
                    run=await _load_run(session, existing.id),
                    created=False,
                )

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
            select(EvaluationRunRow, EvaluationCaseSetRow)
            .join(
                EvaluationCaseSetRow,
                EvaluationCaseSetRow.id == EvaluationRunRow.evaluation_case_set_id,
            )
            .order_by(EvaluationRunRow.sequence.desc())
            .limit(limit + 1)
        )
        if before_sequence is not None:
            statement = statement.where(EvaluationRunRow.sequence < before_sequence)

        async with self._session_factory() as session:
            rows = (await session.execute(statement)).all()

        visible_rows = rows[:limit]
        items = tuple(_stored_summary(run, case_set) for run, case_set in visible_rows)
        next_cursor = items[-1].sequence if len(rows) > limit else None
        return EvaluationRunPage(items=items, next_before_sequence=next_cursor)


async def _ensure_case_set(
    session: AsyncSession,
    submission: EvaluationRunSubmission,
) -> EvaluationCaseSetRow:
    dataset = submission.dataset
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
            created_at=max(submission.created_at, submission.report.generated_at),
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


async def _load_run(session: AsyncSession, run_id: UUID) -> StoredEvaluationRun:
    joined = (
        await session.execute(
            select(EvaluationRunRow, EvaluationCaseSetRow)
            .join(
                EvaluationCaseSetRow,
                EvaluationCaseSetRow.id == EvaluationRunRow.evaluation_case_set_id,
            )
            .where(EvaluationRunRow.id == run_id)
        )
    ).one_or_none()
    if joined is None:
        raise EvaluationRunNotFound(run_id)
    run_row, case_set = joined
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

    summary = _stored_summary(run_row, case_set)
    return StoredEvaluationRun(
        **summary.model_dump(),
        idempotency_key=run_row.idempotency_key,
        report_content_hash=run_row.report_content_hash,
        report=report,
    )


def _stored_summary(
    run_row: EvaluationRunRow,
    case_set: EvaluationCaseSetRow,
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
