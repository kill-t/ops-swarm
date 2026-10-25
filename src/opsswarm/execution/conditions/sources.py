"""核验引用的原证据、人工声明和预检来源边界。"""

from collections.abc import Callable
from datetime import datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from opsswarm.execution.candidate.contracts import PreflightRecord
from opsswarm.execution.candidate.queries import PreflightContext
from opsswarm.execution.candidate.queries import load_context as load_preflight_context
from opsswarm.execution.candidate.queries import read_record as read_preflight
from opsswarm.execution.candidate.tables import CandidatePreflightRow
from opsswarm.execution.conditions.contracts import (
    ConditionError,
    ConditionInput,
    EvidenceReference,
    HumanObservation,
    PreflightReference,
)
from opsswarm.investigation.budget.policy import CallBudgetExceeded
from opsswarm.investigation.persistence.tables import InvestigationRunRow
from opsswarm.investigation.tasks.errors import TaskFactConflict


async def load_context(
    session: AsyncSession, run: InvestigationRunRow, job_id: UUID, *, clock: Callable[[], datetime]
) -> PreflightContext:
    try:
        return await load_preflight_context(session, run, job_id, clock=clock)
    except (ValueError, CallBudgetExceeded, TaskFactConflict) as error:
        raise ConditionError(
            "candidate_condition_source_invalid",
            "核验原批准来源无法完整重建",
            404 if getattr(error, "status_code", None) == 404 else 409,
        ) from error


class ConditionSources:
    """一次受父锁保护的事务内复用预检，原调查来源只遍历一次。"""

    def __init__(self, session: AsyncSession, context: PreflightContext) -> None:
        self._session, self.context = session, context
        self._preflights: dict[UUID, PreflightRecord] = {}

    async def validate(
        self, items: tuple[ConditionInput, ...], *, at: datetime
    ) -> tuple[datetime, ...]:
        decision = self.context.decision.record
        assert decision is not None
        deadlines = []
        for item in items:
            for source in item.sources:
                if isinstance(source, EvidenceReference):
                    if source.evidence_id not in decision.content.evidence_ids:
                        raise ConditionError(
                            "candidate_condition_evidence_invalid", "核验引用不在原计划证据闭包"
                        )
                elif isinstance(source, HumanObservation):
                    if not decision.created_at <= source.observed_at <= at:
                        raise ConditionError(
                            "candidate_condition_observation_invalid",
                            "人工观察时间不属于当前批准后的核验",
                        )
                elif isinstance(source, PreflightReference):
                    report = await self._read_preflight(source.preflight_id)
                    if (
                        report.content_sha256 != source.content_sha256
                        or report.decision_id != decision.id
                        or report.content.observation.error_code is not None
                        or not report.created_at <= at < report.expires_at
                    ):
                        raise ConditionError(
                            "candidate_condition_preflight_invalid", "预检来源不匹配、失败或已到期"
                        )
                    deadlines.append(report.expires_at)
        return tuple(deadlines)

    async def _read_preflight(self, identifier: UUID) -> PreflightRecord:
        if identifier not in self._preflights:
            row = await self._session.get(CandidatePreflightRow, identifier)
            decision = self.context.decision.record
            assert decision is not None
            if (
                row is None
                or row.investigation_run_id != decision.investigation_run_id
                or row.candidate_job_id != decision.candidate_job_id
                or row.decision_id != decision.id
            ):
                raise ConditionError(
                    "candidate_condition_preflight_invalid", "预检不属于当前候选和批准"
                )
            try:
                self._preflights[identifier] = await read_preflight(
                    self._session, row, self.context
                )
            except ValueError as error:
                raise ConditionError(
                    "candidate_condition_preflight_invalid", "预检历史无法完整重建"
                ) from error
        return self._preflights[identifier]
