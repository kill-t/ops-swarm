"""动态处理器的版本边界、图恢复与终端引用选择。"""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from opsswarm.investigation.models import InvestigationMode, InvestigationRequest, WorkItemKind
from opsswarm.investigation.workflow_versions import (
    DYNAMIC_INVESTIGATION_GRAPH_VERSION,
    WorkflowVersion,
)
from opsswarm.worker.dynamic_processor import (
    DynamicInvestigationProcessor,
    WorkflowRoutingProcessor,
)
from opsswarm.worker.runner import ProcessorContext
from opsswarm.worker.settings import InvestigationWorkerSettings
from tests.task_facts_helpers import BASE_TIME


def context(**changes):
    return ProcessorContext(
        request=InvestigationRequest(
            incident_id=uuid4(),
            service_name="order-service",
            title="测试",
            started_at=BASE_TIME,
        ),
        mode=InvestigationMode.MULTI_AGENT,
        work_item_kind=WorkItemKind.RUN_MULTI_AGENT_INVESTIGATION,
        graph_thread_id="thread",
        **{
            "workflow_version": WorkflowVersion.V3,
            "graph_version": DYNAMIC_INVESTIGATION_GRAPH_VERSION,
            **changes,
        },
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("resume", [False, True])
@pytest.mark.parametrize("round_number", [1, 2, 3])
async def test_dynamic_processor_uses_checkpoint_and_highest_route(resume, round_number):
    routes = [
        SimpleNamespace(
            id=uuid4(), content_sha256="a" * 64, decision=SimpleNamespace(route="await_human")
        )
        for _ in range(3)
    ]
    calls = []

    class Graph:
        async def has_checkpoint(self, *, thread_id):
            calls.append(("checkpoint", thread_id))
            return resume

        async def investigate(self, request, *, thread_id):
            calls.append(("investigate", thread_id))
            return self.result()

        async def resume(self, *, thread_id):
            calls.append(("resume", thread_id))
            return self.result()

        def result(self):
            return SimpleNamespace(
                review_route=routes[0],
                follow_up_review_route=routes[1] if round_number >= 2 else None,
                third_round_review_route=routes[2] if round_number == 3 else None,
            )

    processor = DynamicInvestigationProcessor(factory=lambda _: Graph(), model_name="fake")
    outcome = await processor.process(context())
    assert outcome.source_review_route_id == routes[round_number - 1].id
    assert calls == [("checkpoint", "thread"), ("resume" if resume else "investigate", "thread")]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes",
    [
        {"workflow_version": WorkflowVersion.V2},
        {"graph_version": "dynamic-investigation-graph-v14"},
        {"data_source_mode": "live"},
    ],
)
async def test_dynamic_processor_rejects_unsupported_route_before_factory(changes):
    def forbidden(_):
        raise AssertionError("不支持的版本不能创建图")

    with pytest.raises(ValueError, match="当前图"):
        await DynamicInvestigationProcessor(factory=forbidden, model_name="fake").process(
            context(**changes)
        )


@pytest.mark.asyncio
async def test_workflow_router_keeps_legacy_result_contract():
    legacy_result = object()
    dynamic_result = object()

    class Processor:
        def __init__(self, result):
            self.result = result

        async def process(self, context):
            return self.result

    router = WorkflowRoutingProcessor(
        legacy=Processor(legacy_result), dynamic=Processor(dynamic_result)
    )
    assert await router.process(context(workflow_version=WorkflowVersion.V2)) is legacy_result
    assert await router.process(context()) is dynamic_result


@pytest.mark.parametrize(
    "changes",
    [
        {},
        {"processing_timeout_seconds": 179, "lease_duration_seconds": 240},
        {"processing_timeout_seconds": 180, "lease_duration_seconds": 209},
        {
            "processing_timeout_seconds": 240,
            "lease_duration_seconds": 300,
            "data_source_mode": "live",
        },
    ],
)
def test_dynamic_settings_require_explicit_safe_processing_window(changes):
    with pytest.raises(ValidationError, match="v3 需要"):
        InvestigationWorkerSettings(enable_dynamic_workflow=True, **changes)


def test_dynamic_settings_allow_explicit_longer_frozen_window():
    settings = InvestigationWorkerSettings(
        enable_dynamic_workflow=True, processing_timeout_seconds=240, lease_duration_seconds=300
    )
    assert settings.enable_dynamic_workflow


def test_worker_frozen_source_is_stable_scoped_and_preserves_baseline():
    from opsswarm.investigation.scenarios import RELEASE_500_SCENARIO
    from opsswarm.worker.dynamic_factory import worker_frozen_source

    request = context().request
    source = worker_frozen_source(request)
    repeated = worker_frozen_source(request)
    assert source.snapshot_time == request.started_at
    assert source.snapshot_id == repeated.snapshot_id
    assert (
        source.snapshot_id
        != worker_frozen_source(request.model_copy(update={"incident_id": uuid4()})).snapshot_id
    )
    assert source.legacy_snapshot_ids == frozenset()
    observed = source.query_logs(service_name="order-service", lookback_minutes=5)
    assert observed[0].source == "frozen-log-store"
    assert observed[0].observed_at == request.started_at
    assert RELEASE_500_SCENARIO.evidence[0].observed_at != request.started_at
    with pytest.raises(ValueError, match="发布故障演练"):
        worker_frozen_source(request.model_copy(update={"service_name": "other"}))
