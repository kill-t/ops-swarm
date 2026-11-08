"""历史公开工厂的版本、拓扑、按需装配与共享预算兼容。"""

from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from opsswarm.investigation.budget.policy import CallBudgetExceeded
from opsswarm.worker import dynamic_graph as legacy
from opsswarm.worker.dynamic_factory import worker_frozen_source
from tests.unit.investigation.test_dynamic_factory import context

FACTORIES = (
    ("DynamicCoordinationGraphFactory", "DynamicCoordinationGraph"),
    ("DynamicFirstRoundGraphFactory", "DynamicFirstRoundGraph"),
    ("DynamicSynthesisGraphFactory", "DynamicSynthesisGraph"),
    ("DynamicReviewGraphFactory", "DynamicReviewGraph"),
    ("DynamicRoutingGraphFactory", "DynamicRoutingGraph"),
    ("DynamicFollowUpGraphFactory", "DynamicFollowUpGraph"),
    ("DynamicSecondRoundGraphFactory", "DynamicSecondRoundGraph"),
    ("DynamicSecondRoundSynthesisGraphFactory", "DynamicSecondRoundSynthesisGraph"),
    ("DynamicSecondRoundReviewGraphFactory", "DynamicSecondRoundReviewGraph"),
    ("DynamicSecondRoundRoutingGraphFactory", "DynamicSecondRoundRoutingGraph"),
    ("DynamicThirdRoundPlanningGraphFactory", "DynamicThirdRoundPlanningGraph"),
    ("DynamicThirdRoundGraphFactory", "DynamicThirdRoundGraph"),
    ("DynamicThirdRoundSynthesisGraphFactory", "DynamicThirdRoundSynthesisGraph"),
    ("DynamicThirdRoundReviewGraphFactory", "DynamicThirdRoundReviewGraph"),
)
CASES = tuple((index, *item) for index, item in enumerate(FACTORIES, start=1))
NODES = (
    "coordinate",
    "investigate_specialist",
    "synthesize_open_hypothesis",
    "review_open_hypothesis",
    "determine_review_route",
    "compile_follow_up_plan",
    "investigate_follow_up_specialist",
    "synthesize_follow_up_hypothesis",
    "review_follow_up_hypothesis",
    "route_follow_up_review",
    "compile_third_round_plan",
    "investigate_third_round_specialist",
    "synthesize_third_round_hypothesis",
    "review_third_round_hypothesis",
)


def setup_factory(version, factory_name, *, source=None, model=None):
    run = context(live=False).model_copy(
        update={"graph_version": f"dynamic-investigation-graph-v{version}"}
    )
    sessions = MagicMock(side_effect=AssertionError("装配不应执行数据库用例"))
    saver = InMemorySaver()
    create = getattr(legacy, factory_name)(
        session_factory=sessions,
        data_source=worker_frozen_source(run.request) if source is None else source,
        model=model or SimpleNamespace(model_name="fake-budget", max_retries=0, n=1),
        checkpointer=saver,
        clock=lambda: run.request.started_at,
    )
    return create, run, sessions, saver


@pytest.mark.parametrize("version,factory_name,graph_name", CASES)
def test_published_factories_keep_their_exact_graph_frontier(version, factory_name, graph_name):
    create, run, sessions, saver = setup_factory(version, factory_name)
    graph = create(run)
    assert isinstance(graph, getattr(legacy, graph_name))
    assert set(graph._workflow.get_graph().nodes) == {"__start__", "__end__", *NODES[:version]}
    assert graph._workflow.checkpointer is saver
    assert graph._run_id == run.investigation_run_id
    if version >= 2:
        runner = graph._runtime._call_runner
        assert runner._run_id == run.investigation_run_id
        assert runner._lease_token == run.lease_token
        assert graph._analyst._runner is runner
        for stage in (
            "_synthesizer",
            "_reviewer",
            "_follow_up_synthesizer",
            "_follow_up_reviewer",
            "_third_round_synthesizer",
            "_third_round_reviewer",
        ):
            if hasattr(graph, stage):
                assert getattr(graph, stage)._runner is runner
    sessions.assert_not_called()
    assert not saver.storage


@pytest.mark.parametrize("version,factory_name,graph_name", CASES)
def test_each_legacy_factory_rejects_upgrading_to_current_graph(version, factory_name, graph_name):
    create, run, sessions, saver = setup_factory(version, factory_name)
    with pytest.raises(ValueError, match="v3"):
        create(run.model_copy(update={"graph_version": "dynamic-investigation-graph-v15"}))
    sessions.assert_not_called()
    assert not saver.storage


@pytest.mark.parametrize(
    "change",
    [
        {"lease_token": None},
        {"graph_thread_id": "another-run"},
        {"data_source_mode": "live"},
        {"model_budget": None},
    ],
)
def test_shared_frozen_admission_keeps_rejecting_incomplete_or_live_context(change):
    create, run, sessions, saver = setup_factory(14, FACTORIES[-1][0])
    with pytest.raises(ValueError, match="冻结运行契约"):
        create(run.model_copy(update=change))
    sessions.assert_not_called()
    assert not saver.storage


def test_shared_model_admission_rejects_sdk_retries():
    create, run, sessions, saver = setup_factory(
        14,
        FACTORIES[-1][0],
        model=SimpleNamespace(model_name="fake-budget", max_retries=1, n=1),
    )
    with pytest.raises(CallBudgetExceeded):
        create(run)
    sessions.assert_not_called()
    assert not saver.storage


@pytest.mark.parametrize(
    "version,forbidden",
    [
        (1, ("BudgetedCallRunner", "DynamicTaskRuntime", "FirstRoundSynthesizer")),
        (10, ("ThirdRoundPlanService", "ThirdRoundSynthesizer", "ThirdRoundHypothesisReviewer")),
    ],
)
def test_assembly_does_not_create_stages_outside_the_old_graph(version, forbidden, monkeypatch):
    def reject(*args, **kwargs):
        raise AssertionError("尚未进入旧图的阶段不能提前装配")

    for name in forbidden:
        monkeypatch.setattr(legacy, name, reject)
    create, run, sessions, saver = setup_factory(version, FACTORIES[version - 1][0])
    create(run)
    sessions.assert_not_called()
    assert not saver.storage


def test_legacy_factory_keeps_rejecting_non_frozen_source():
    with pytest.raises(ValueError, match="冻结场景数据源"):
        setup_factory(1, FACTORIES[0][0], source=object())


def test_factory_does_not_reuse_assembly_or_budget_runner_after_a_new_lease():
    create, run, sessions, saver = setup_factory(14, FACTORIES[-1][0])
    first = create(run)
    renewed = run.model_copy(update={"lease_token": uuid4()})
    second = create(renewed)
    assert first._runtime._call_runner is not second._runtime._call_runner
    assert first._runtime._call_runner._lease_token == run.lease_token
    assert second._runtime._call_runner._lease_token == renewed.lease_token
    sessions.assert_not_called()
    assert not saver.storage
