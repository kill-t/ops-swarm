"""原生预算门槛必须同时保留原每任务工具次数限制。"""

import json
from uuid import uuid4

import pytest
from sqlalchemy import text

from opsswarm.investigation.budget.contracts import CallBudgetPolicy
from opsswarm.investigation.reinvestigation.policy import budget_blockers
from tests.unit.investigation.test_reinvestigation_admission_policy import NOW, call, snapshot

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def test_per_task_tool_limit_matches_original_ledger_policy(database_session_factory):
    task = uuid4()
    calls = (call(kind="tool", task_id=task), call(kind="tool", task_id=task, sequence=2))
    for limit, expected in ((1, "tool_call_budget_exceeded"), (2, None)):
        budget = snapshot(
            calls, policy=CallBudgetPolicy(max_tool_calls=10, max_tool_calls_per_task=limit)
        )
        blockers = budget_blockers(budget, now=NOW)
        assert (blockers[0] if blockers else None) == expected
        async with database_session_factory() as session:
            actual = await session.scalar(
                text("SELECT reinvestigation_budget_blocker(CAST(:budget AS jsonb),:now)"),
                {"budget": json.dumps(budget.model_dump(mode="json")), "now": NOW},
            )
        assert actual == expected
