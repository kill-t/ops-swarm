"""Alembic 异步迁移入口。"""

import asyncio
import sys
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context
from opsswarm.approval.candidate import tables as candidate_approval_tables  # noqa: F401
from opsswarm.approval.conditions import tables as condition_approval_tables  # noqa: F401
from opsswarm.approval.decision import tables as candidate_decision_tables  # noqa: F401
from opsswarm.approval.persistence import tables as approval_tables  # noqa: F401
from opsswarm.change_records.persistence import tables as change_record_tables  # noqa: F401
from opsswarm.database.asyncio_compat import create_selector_event_loop
from opsswarm.database.base import Base
from opsswarm.database.settings import DatabaseSettings
from opsswarm.evaluation.persistence import tables as evaluation_tables  # noqa: F401
from opsswarm.execution.candidate import tables as candidate_preflight_tables  # noqa: F401
from opsswarm.execution.conditions import tables as candidate_condition_tables  # noqa: F401
from opsswarm.execution.persistence import tables as execution_tables  # noqa: F401
from opsswarm.execution.release import tables as candidate_release_tables  # noqa: F401
from opsswarm.experience import context_tables as experience_context_tables  # noqa: F401
from opsswarm.experience import lifecycle_tables as experience_lifecycle_tables  # noqa: F401
from opsswarm.experience import replacement_tables as experience_replacement_tables  # noqa: F401
from opsswarm.experience import review_tables as experience_review_tables  # noqa: F401
from opsswarm.experience import tables as experience_tables  # noqa: F401
from opsswarm.fault_lab.release import tables as lab_release_tables  # noqa: F401
from opsswarm.follow_up.persistence import tables as follow_up_tables  # noqa: F401
from opsswarm.identity import tables as operator_identity_tables  # noqa: F401
from opsswarm.incidents.persistence import tables  # noqa: F401
from opsswarm.investigation.budget import tables as investigation_budget_tables  # noqa: F401
from opsswarm.investigation.candidate import tables as candidate_tables  # noqa: F401
from opsswarm.investigation.coordination import tables as coordination_tables  # noqa: F401
from opsswarm.investigation.handoff import tables as handoff_tables  # noqa: F401
from opsswarm.investigation.human_prepare import tables as human_preparation_tables  # noqa: F401
from opsswarm.investigation.human_review import tables as human_review_tables  # noqa: F401
from opsswarm.investigation.human_routing import tables as human_routing_tables  # noqa: F401
from opsswarm.investigation.human_wait import tables as human_wait_tables  # noqa: F401
from opsswarm.investigation.persistence import tables as investigation_tables  # noqa: F401
from opsswarm.investigation.review import tables as review_tables  # noqa: F401
from opsswarm.investigation.review_routing import tables as review_routing_tables  # noqa: F401
from opsswarm.investigation.synthesis import tables as synthesis_tables  # noqa: F401
from opsswarm.investigation.tasks import artifact_tables as task_artifact_tables  # noqa: F401
from opsswarm.investigation.tasks import evidence_tables as task_evidence_tables  # noqa: F401
from opsswarm.investigation.tasks import tables as investigation_task_tables  # noqa: F401
from opsswarm.investigation.working_summary import tables as working_summary_tables  # noqa: F401
from opsswarm.planning.candidate import tables as candidate_plan_tables  # noqa: F401
from opsswarm.planning.persistence import tables as planning_tables  # noqa: F401
from opsswarm.reports.persistence import tables as report_tables  # noqa: F401
from opsswarm.verification.persistence import tables as verification_tables  # noqa: F401

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

settings = DatabaseSettings()
config.set_main_option("sqlalchemy.url", settings.database_url.replace("%", "%%"))
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """在不创建数据库连接时生成迁移 SQL。"""

    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: object) -> None:
    """在 Alembic 提供的同步桥接连接上执行迁移。"""

    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """创建异步引擎并把迁移委托给同步桥接层。"""

    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """连接目标数据库执行迁移。"""

    loop_factory = create_selector_event_loop if sys.platform == "win32" else None
    asyncio.run(run_async_migrations(), loop_factory=loop_factory)


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
