"""带受限反序列化的异步 PostgreSQL checkpointer。"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from opsswarm.investigation.models import (
    EvidenceKind,
    EvidenceObservation,
    InvestigationConclusion,
    InvestigationRequest,
    Recommendation,
    RootCause,
    SpecialistReport,
)


def _checkpoint_serializer() -> JsonPlusSerializer:
    """只允许恢复图状态中明确使用的项目类型，禁止任意类型加载。"""

    return JsonPlusSerializer(
        allowed_msgpack_modules=(
            EvidenceKind,
            EvidenceObservation,
            InvestigationConclusion,
            InvestigationRequest,
            Recommendation,
            RootCause,
            SpecialistReport,
        )
    )


@asynccontextmanager
async def open_checkpoint_saver(database_url: str) -> AsyncIterator[AsyncPostgresSaver]:
    """为一个进程生命周期打开并最终关闭异步 checkpointer。"""

    async with AsyncPostgresSaver.from_conn_string(
        database_url,
        serde=_checkpoint_serializer(),
    ) as saver:
        yield saver


async def initialize_checkpoint_schema(database_url: str) -> None:
    """执行 LangGraph 自带的幂等 checkpoint schema 迁移。"""

    async with open_checkpoint_saver(database_url) as saver:
        await saver.setup()
