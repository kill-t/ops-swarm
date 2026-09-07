"""现场材料契约与冻结历史兼容不依赖模型判断。"""

from datetime import timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError

from opsswarm.investigation.live import LiveInvestigationDataSource
from opsswarm.investigation.live_window import UnsupportedLiveTaskSource
from opsswarm.investigation.scenarios import RELEASE_500_SCENARIO, FrozenScenarioDataSource
from opsswarm.investigation.tasks.evidence import (
    TaskEvidenceSnapshot,
    evidence_hash,
    live_snapshot_id,
)
from tests.live_task_helpers import LiveHttpFixture
from tests.task_evidence_helpers import snapshot
from tests.task_facts_helpers import BASE_TIME, TaskClock


def live_material():
    fixture = LiveHttpFixture(BASE_TIME, TaskClock())
    source = fixture.source.bind_window(service_name="order-service", cutoff=BASE_TIME)
    return TaskEvidenceSnapshot(
        version="task-evidence-live-v1",
        data_source_mode="live",
        evidence_snapshot_id=live_snapshot_id(uuid4(), BASE_TIME),
        role="log",
        service_name="order-service",
        window_start=BASE_TIME - timedelta(minutes=15),
        window_end=BASE_TIME,
        evidence=tuple(source.query_logs(service_name="order-service", lookback_minutes=15)),
    )


@pytest.mark.parametrize("invalid", ["policy", "mode", "source", "service", "window", "missing"])
def test_live_snapshot_rejects_frozen_or_mismatched_material(invalid):
    body = live_material().model_dump(mode="json")
    if invalid == "policy":
        body["version"] = "task-evidence-v1"
    elif invalid == "mode":
        body["data_source_mode"] = "frozen"
    elif invalid == "source":
        body["evidence"][0]["source"] = "frozen-scenario"
    elif invalid == "service":
        body["evidence"][0]["attributes"]["service_name"] = "another-service"
    elif invalid == "window":
        body["evidence"][0]["attributes"]["window_end"] = (
            BASE_TIME + timedelta(seconds=1)
        ).isoformat()
    else:
        del body["evidence"][0]["attributes"]["window_start"]
    with pytest.raises(ValidationError):
        TaskEvidenceSnapshot.model_validate(body)


@pytest.mark.parametrize(
    "method,arg",
    [
        ("query_logs", "lookback_minutes"),
        ("query_metrics", "lookback_minutes"),
        ("list_recent_changes", "lookback_hours"),
    ],
)
@pytest.mark.parametrize("invalid", ["service", "duration"])
def test_bound_queries_cannot_expand_scope_before_http(method, arg, invalid):
    fixture = LiveHttpFixture(BASE_TIME, TaskClock())
    bound = fixture.source.bind_window(service_name="order-service", cutoff=BASE_TIME)
    duration = 15 if arg == "lookback_minutes" else 6
    with pytest.raises(ValueError, match="不能改变"):
        getattr(bound, method)(
            service_name="other" if invalid == "service" else "order-service",
            **{arg: 1 if invalid == "duration" else duration},
        )
    assert fixture.requests == []


def test_unsupported_composition_is_rejected_before_queries():
    source = LiveInvestigationDataSource(changes=FrozenScenarioDataSource(RELEASE_500_SCENARIO))
    with pytest.raises(UnsupportedLiveTaskSource):
        source.bind_window(service_name="order-service", cutoff=BASE_TIME)


def test_frozen_snapshot_serialization_has_no_new_fields():
    material = snapshot().model_copy(
        update={"evidence_snapshot_id": "frozen:compatibility-fixture"}
    )
    # 单独固定既有范围 ID，验证材料格式兼容，不依赖旧场景身份算法。
    # 旧 JSON 仍以完全相同的字段集合生成散列，不因新增现场模式而隐式升级。
    assert set(material.model_dump()) == {
        "version",
        "data_source_mode",
        "evidence_snapshot_id",
        "role",
        "service_name",
        "window_start",
        "window_end",
        "evidence",
    }
    assert (
        evidence_hash(material)
        == "52527c3c3aa4e785ac2ce2ec278c195a789cdace5c54ea96a85bb8a03ae657fe"
    )
    assert live_snapshot_id(uuid4(), BASE_TIME) != live_snapshot_id(uuid4(), BASE_TIME)
