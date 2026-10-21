"""真实 PostgreSQL 验证补查计划来源迁移与 ORM 一致。"""

import pytest
from sqlalchemy import text

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def test_follow_up_plan_source_schema_is_present(database_session_factory) -> None:
    async with database_session_factory() as session:
        head = await session.scalar(text("SELECT version_num FROM alembic_version"))
        column = (
            await session.execute(
                text(
                    "SELECT is_nullable, data_type FROM information_schema.columns "
                    "WHERE table_name='investigation_coordination_plans' "
                    "AND column_name='source_review_route_id'"
                )
            )
        ).one()
        constraints = set(
            await session.scalars(
                text(
                    "SELECT conname FROM pg_constraint "
                    "WHERE conrelid IN "
                    "('investigation_coordination_plans'::regclass, "
                    "'investigation_hypothesis_review_routes'::regclass)"
                )
            )
        )

    assert head == "0068_human_wait_commands"
    assert column == ("YES", "uuid")
    assert {
        "fk_coordination_plans_source_review_route",
        "uq_coordination_plans_source_review_route",
        "ck_coordination_plans_source_exclusive",
        "uq_hypothesis_review_routes_id_run",
    } <= constraints
