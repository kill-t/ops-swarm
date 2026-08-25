"""使用独立真实 PostgreSQL 数据库验证 LangGraph 节点级恢复。"""

from uuid import uuid4

import pytest

from opsswarm.checkpoints import initialize_checkpoint_schema, open_checkpoint_saver
from opsswarm.investigation.graph_thread import build_graph_thread_id
from opsswarm.investigation.models import EvidenceKind, HypothesisStatus
from opsswarm.investigation.parallel_graph import LangGraphParallelInvestigator
from opsswarm.investigation.scenarios import RELEASE_500_SCENARIO
from tests.checkpoint_fakes import (
    ConclusionSynthesizerStub,
    CountingDataSource,
    SpecialistAnalystStub,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def test_postgres_checkpoint_resumes_only_failed_parallel_node(
    checkpoint_database_url: str,
) -> None:
    await initialize_checkpoint_schema(checkpoint_database_url)
    await initialize_checkpoint_schema(checkpoint_database_url)
    thread_id = build_graph_thread_id(uuid4())
    first_data_source = CountingDataSource()

    async with open_checkpoint_saver(checkpoint_database_url) as first_saver:
        first_graph = LangGraphParallelInvestigator(
            data_source=first_data_source,
            analyst=SpecialistAnalystStub(fail_logs=True),
            synthesizer=ConclusionSynthesizerStub(),
            checkpointer=first_saver,
        )
        with pytest.raises(RuntimeError, match="模拟日志节点进程中断"):
            await first_graph.investigate(
                RELEASE_500_SCENARIO.request,
                thread_id=thread_id,
            )

    assert first_data_source.calls == {
        EvidenceKind.LOG: 1,
        EvidenceKind.METRIC: 1,
        EvidenceKind.CHANGE: 1,
    }

    recovered_data_source = CountingDataSource()
    async with open_checkpoint_saver(checkpoint_database_url) as second_saver:
        recovered_graph = LangGraphParallelInvestigator(
            data_source=recovered_data_source,
            analyst=SpecialistAnalystStub(),
            synthesizer=ConclusionSynthesizerStub(),
            checkpointer=second_saver,
        )
        outcome = await recovered_graph.resume(thread_id=thread_id)
        latest_checkpoint = await second_saver.aget_tuple(
            {"configurable": {"thread_id": thread_id}}
        )

    assert recovered_data_source.calls == {
        EvidenceKind.LOG: 1,
        EvidenceKind.METRIC: 0,
        EvidenceKind.CHANGE: 0,
    }
    assert outcome.result.tool_call_count == 3
    assert set(outcome.result.returned_evidence_ids) == RELEASE_500_SCENARIO.required_evidence_ids
    assert latest_checkpoint is not None
    assert latest_checkpoint.checkpoint["channel_values"]["conclusion"] == outcome.result.conclusion
    assert outcome.hypothesis_challenge.status is HypothesisStatus.SUPPORTED
    assert (
        latest_checkpoint.checkpoint["channel_values"]["hypothesis_challenge"]
        == outcome.hypothesis_challenge
    )
