"""回流候选元数据和空库迁移往返。"""

import asyncio

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from alembic import command
from opsswarm.investigation.candidate.output import CandidateModelResult
from opsswarm.investigation.reinvestigation_candidate.policy import evaluate_candidate
from opsswarm.planning.models import ActionType
from tests.integration.test_candidate_approval_migration import config
from tests.unit.investigation.test_reinvestigation_candidate import proposal, source_fixture

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def test_0093_metadata_and_empty_roundtrip(database_session_factory):
    await asyncio.to_thread(command.check, config())
    try:
        await asyncio.to_thread(command.downgrade, config(), "0092_reflow_routing")
        async with database_session_factory() as session:
            assert (
                await session.scalar(
                    text("SELECT to_regclass('investigation_reflow_candidate_jobs')")
                )
                is None
            )
    finally:
        await asyncio.to_thread(command.upgrade, config(), "head")
    await asyncio.to_thread(command.check, config())


async def test_native_evaluation_matches_domain_and_rejects_new_evidence_escape(
    database_session_factory,
):
    source = source_fixture(extra_evidence=True)
    proposals = [proposal(source, action_type=action) for action in ActionType]
    proposals.append(
        proposal(
            source,
            status="unsupported",
            action_type=None,
            evidence_ids=(),
            preconditions=(),
        )
    )
    async with database_session_factory() as session, session.begin():
        for item in proposals:
            native = await session.scalar(
                text("SELECT reflow_candidate_evaluation(CAST(:p AS jsonb),CAST(:s AS jsonb))"),
                {"p": item.model_dump_json(), "s": source.model_dump_json()},
            )
            expected = evaluate_candidate(source, CandidateModelResult(proposal=item))
            assert native == expected.model_dump(mode="json")
        for evidence_id in ("historical-only", "new-change-extra"):
            with pytest.raises(DBAPIError):
                async with session.begin_nested():
                    await session.execute(
                        text(
                            "SELECT assert_reflow_candidate_proposal("
                            "CAST(:p AS jsonb),CAST(:s AS jsonb))"
                        ),
                        {
                            "p": proposal(source, evidence_ids=(evidence_id,)).model_dump_json(),
                            "s": source.model_dump_json(),
                        },
                    )
        forged = evaluate_candidate(source, CandidateModelResult(proposal=proposal(source)))
        with pytest.raises(DBAPIError):
            async with session.begin_nested():
                await session.execute(
                    text(
                        "SELECT reflow_candidate_evaluation(CAST(:p AS jsonb),CAST(:s AS jsonb),"
                        "CAST(:e AS jsonb))"
                    ),
                    {
                        "p": proposal(source).model_dump_json(),
                        "s": source.model_dump_json(),
                        "e": forged.model_copy(
                            update={"execution_eligible": True}
                        ).model_dump_json(),
                    },
                )
