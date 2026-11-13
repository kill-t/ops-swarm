"""用现有 API 核对验证失败后的一次再调查和人工接管。"""

import time
from datetime import datetime

from opsswarm.investigation.context.hashing import content_hash
from opsswarm.investigation.reinvestigation_review.contracts import ReflowReviewResult
from scripts.local_demo.client import require
from scripts.local_demo.config import CHANGE, ORDER
from scripts.local_demo.investigation import Investigation

STAGE = "reinvestigation"


def _admit_once(flow: Investigation, convergence: dict) -> dict:
    path = flow.base + "/reinvestigation-admission"
    body = dict(
        action="request_reinvestigation",
        convergence_sha256=convergence["content_sha256"],
        expected_incident_version=convergence["after_version"],
    )
    flow.post(path, body, status=201)
    flow.post(path, body, status=200)
    flow.post(path, body, status=409, suffix="second-admission")
    admitted = flow.client.call(path)["record"]
    require(admitted is not None, STAGE, "admission_not_persisted")
    commands = flow.client.call(path + "/commands")["items"]
    require(
        [(item["outcome"], item["request_id"], item["error_code"]) for item in commands]
        == [
            ("applied", admitted["id"], None),
            ("replayed", admitted["id"], None),
            ("error", None, "reinvestigation_already_exists"),
        ],
        STAGE,
        "admission_audit_mismatch",
    )
    return admitted


def _verify_new_change(flow: Investigation, change: dict, routed: dict) -> dict:
    tasks = flow.client.call(flow.base + "/reinvestigation-execution")["tasks"]
    evidence_id = "change-" + change["record_id"]
    task = next(
        (
            item
            for item in tasks
            if item["status"] == "succeeded"
            and item["evidence"]["data_source_mode"] == "live"
            and item["evidence"]["role"] == "change"
            and any(
                evidence["evidence_id"] == evidence_id for evidence in item["evidence"]["evidence"]
            )
            and evidence_id in item["result"]["report"]["evidence_ids"]
        ),
        None,
    )
    require(task is not None, STAGE, "new_change_not_in_professional_report")
    window = task["effective_window"]["window"]
    require(
        datetime.fromisoformat(window["window_start"])
        <= datetime.fromisoformat(change["occurred_at"])
        <= datetime.fromisoformat(window["window_end"])
        and dict(task_id=task["task_id"], effective_window=task["effective_window"])
        in routed["observation_windows"],
        STAGE,
        "new_change_outside_routed_window",
    )
    reviewed = flow.client.call(flow.base + "/reinvestigation-review")
    require(reviewed["status"] == "succeeded", STAGE, "review_not_succeeded")
    review = ReflowReviewResult.model_validate(reviewed["result"])
    require(
        evidence_id in review.proposal.accepted_supporting_evidence_ids
        and content_hash(review) == routed["review_result_sha256"],
        STAGE,
        "new_change_not_in_routed_review",
    )
    return dict(
        evidence_id=evidence_id,
        task_id=task["task_id"],
        window=window,
        review_job_id=str(review.job_id),
        review_call_id=str(review.source_call_id),
        review_result_sha256=routed["review_result_sha256"],
    )


def reinvestigate(flow: Investigation, convergence: dict) -> None:
    c = flow.client
    release_before = c.call("/__admin/simulated-release", origin=ORDER)
    # 分派窗口向内取整。给验证时刻和实际取证截止各留出一个完整秒。
    time.sleep(1.1)
    c.call(
        "/__admin/configuration/database-pool",
        dict(value=2, expected_value=1),
        method="PUT",
        origin=ORDER,
    )
    changed = c.call(
        "/__admin/changes",
        dict(
            change_type="configuration",
            external_change_id="pool-" + flow.case_id,
            key="database_pool_size",
            from_value=1,
            to_value=2,
        ),
        origin=CHANGE,
    )["record"]
    time.sleep(1.1)
    before = c.call(flow.base + "/call-budget")
    admitted = _admit_once(flow, convergence)
    flow.emit(
        "reinvestigation_admitted",
        admission_id=admitted["id"],
        change_record_id=changed["record_id"],
        second_admission_rejected=True,
    )
    routed = c.poll(
        STAGE,
        lambda: c.call(flow.base + "/reinvestigation-routing"),
        lambda value: value["record"] is not None,
    )["record"]
    require(
        routed["route"] == "await_human"
        and routed["dispatch_status"] == "not_dispatched"
        and "live_reflow_manual_takeover" in routed["blocking_reason_codes"],
        STAGE,
        "not_persisted_manual_takeover",
    )
    verified = _verify_new_change(flow, changed, routed)
    after = c.call(flow.base + "/call-budget")
    require(
        before["deadline"] == after["deadline"]
        and before["policy"] == after["policy"]
        and before["model_budget"] == after["model_budget"]
        and after["calls"][: len(before["calls"])] == before["calls"]
        and len(after["calls"]) > len(before["calls"])
        and all(call["status"] != "reserved" for call in after["calls"]),
        STAGE,
        "original_budget_not_preserved",
    )
    require(
        c.call(flow.incident)["status"] == "investigating",
        STAGE,
        "incident_not_investigating",
    )
    release_after = c.call("/__admin/simulated-release", origin=ORDER)
    require(release_before == release_after, STAGE, "release_changed_during_reflow")
    c.call(
        "/__admin/configuration/database-pool",
        dict(value=50, expected_value=2),
        method="PUT",
        origin=ORDER,
    )
    flow.emit(
        STAGE,
        admission_id=admitted["id"],
        route=routed["route"],
        route_created_at=routed["created_at"],
        deadline=after["deadline"],
        same_deadline=True,
        calls_before=len(before["calls"]),
        calls_after=len(after["calls"]),
        release_revision=release_after["revision"],
        **verified,
    )
