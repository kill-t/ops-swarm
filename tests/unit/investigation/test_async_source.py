"""异步桥接保留来源校验，并让固定图真正并发取证。"""

import asyncio

import httpx2
import pytest

from opsswarm.investigation.async_source import ThreadedInvestigationDataSource
from opsswarm.investigation.errors import (
    InvestigationDataSourceFailure,
    UnsupportedInvestigationTarget,
)
from opsswarm.investigation.live import LiveInvestigationDataSource
from opsswarm.investigation.models import EvidenceKind
from opsswarm.investigation.parallel_graph import LangGraphParallelInvestigator
from opsswarm.investigation.query_execution import EvidenceQueryTimeout, ReadOnlyQueryExecutor
from opsswarm.investigation.scenarios import RELEASE_500_SCENARIO
from tests.async_evidence_helpers import BlockedQuery, drain
from tests.checkpoint_fakes import ConclusionSynthesizerStub, SpecialistAnalystStub
from tests.unit.investigation.test_live import PortSpy
from tests.unit.investigation.test_loki import _source, _transport


class BlockedPort(PortSpy):
    def __init__(self, kind):
        super().__init__(kind)
        self.gate = BlockedQuery()

    def query_logs(self, **kwargs):
        self.gate()
        return super().query_logs(**kwargs)

    def query_metrics(self, **kwargs):
        self.gate()
        return super().query_metrics(**kwargs)

    def list_recent_changes(self, **kwargs):
        self.gate()
        return super().list_recent_changes(**kwargs)


@pytest.mark.asyncio
async def test_live_graph_fans_out_blocking_sources_before_any_analysis():
    ports = [
        BlockedPort(kind) for kind in (EvidenceKind.LOG, EvidenceKind.METRIC, EvidenceKind.CHANGE)
    ]
    executor = ReadOnlyQueryExecutor()
    source = LiveInvestigationDataSource(logs=ports[0], metrics=ports[1], changes=ports[2])
    graph = LangGraphParallelInvestigator(
        data_source=source,
        query_executor=executor,
        analyst=SpecialistAnalystStub(),
        synthesizer=ConclusionSynthesizerStub(),
    )
    task = asyncio.create_task(graph.investigate(RELEASE_500_SCENARIO.request))
    try:
        await asyncio.wait_for(asyncio.gather(*(p.gate.entered.wait() for p in ports)), 2)
        assert not task.done()
        await drain(executor, *(p.gate for p in ports))
        result = await task
        assert set(result.result.returned_evidence_ids) == {
            "log-live",
            "metric-live",
            "change-live",
        }
        assert [p.calls for p in ports] == [
            [("order-service", 15)],
            [("order-service", 15)],
            [("order-service", 6)],
        ]
    finally:
        await drain(executor, *(p.gate for p in ports))
        executor.close()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [False, True])
async def test_real_http_adapter_remains_validated_through_async_port(failure):
    transport, requests = _transport(status_code=503 if failure else 200)
    executor = ReadOnlyQueryExecutor()
    source = ThreadedInvestigationDataSource(_source(transport), executor)
    try:
        with pytest.raises(UnsupportedInvestigationTarget):
            await source.query_logs(service_name="other-service", lookback_minutes=15)
        with pytest.raises(ValueError):
            await source.query_logs(service_name="order-service", lookback_minutes=61)
        assert requests == []
        if failure:
            with pytest.raises(InvestigationDataSourceFailure) as caught:
                await source.query_logs(service_name="order-service", lookback_minutes=15)
            assert caught.value.error_code == "loki_unexpected_status"
        else:
            evidence = await source.query_logs(service_name="order-service", lookback_minutes=15)
            assert len(evidence) == 3 and {e.source for e in evidence} == {"loki-fault-lab"}
        assert len(requests) == 1
    finally:
        executor.close()


@pytest.mark.asyncio
async def test_blocking_http_transport_timeout_does_not_return_late_evidence():
    original, _ = _transport()
    gate = BlockedQuery()

    def handler(request):
        gate()
        return original.handle_request(request)

    executor = ReadOnlyQueryExecutor(timeout_seconds=0.1)
    source = ThreadedInvestigationDataSource(_source(httpx2.MockTransport(handler)), executor)
    task = asyncio.create_task(source.query_logs(service_name="order-service", lookback_minutes=15))
    try:
        await asyncio.wait_for(gate.entered.wait(), 2)
        with pytest.raises(EvidenceQueryTimeout):
            await task
        assert not gate.finished.is_set()
        await drain(executor, gate)
        assert isinstance(task.exception(), EvidenceQueryTimeout)
    finally:
        await drain(executor, gate)
        executor.close()
        await asyncio.gather(task, return_exceptions=True)


def test_live_graph_requires_explicit_query_executor():
    with pytest.raises(ValueError, match="受限取证执行器"):
        LangGraphParallelInvestigator(
            data_source=LiveInvestigationDataSource(),
            analyst=SpecialistAnalystStub(),
            synthesizer=ConclusionSynthesizerStub(),
        )
