"""可信审批、唯一处置与客观恢复验证的演示用例。"""

import time
from datetime import datetime

from scripts.local_demo.client import require
from scripts.local_demo.config import ORDER
from scripts.local_demo.environment import ComposeControl
from scripts.local_demo.investigation import Investigation


def approve_and_verify(flow: Investigation, path: str) -> str:
    c = flow.client
    job = c.call(path)
    wait = dict(expected_wait_version=job["wait_version"])
    plan_path = path + "/action-plan"
    flow.post(
        plan_path,
        dict(
            action="create_plan",
            **flow.version(),
            **wait,
            candidate_result_sha256=job["result"]["content_sha256"],
        ),
    )
    plan = c.call(plan_path)["record"]
    approval = plan_path + "/approval-request"
    flow.post(
        approval,
        dict(
            action="request_approval",
            **flow.version(),
            **wait,
            plan_content_sha256=plan["content_sha256"],
            acknowledge_unresolved_conditions=True,
        ),
    )
    request = c.call(approval)["record"]
    decision = dict(
        action="approve",
        **flow.version(),
        request_id=request["id"],
        plan_content_sha256=request["plan_content_sha256"],
        acknowledge_unresolved_conditions=True,
    )
    flow.post(approval + "/decision", decision, status=403)
    flow.post(approval + "/decision", decision, role="approver")
    approved = c.call(approval + "/decision")["record"]
    require(approved["outcome"] == "approved", "approval", "plan_not_approved")
    conditions_path = path + "/execution-conditions"
    conditions = c.call(conditions_path)
    require(len(conditions["catalog"]) == 1, "conditions", "unsupported_condition_catalog")
    require(
        conditions["catalog"][0]["text"] == "确认当前演练版本与发布依据一致",
        "conditions",
        "unsupported_condition_meaning",
    )
    state = c.call("/__admin/simulated-release", origin=ORDER)
    require(state["version"] == "2026.01.01-rc1", "conditions", "lab_version_changed")
    approved_at = datetime.fromisoformat(approved["created_at"])
    observed_at = c.observe_order_failure()
    # HTTP Date 精确到秒。重新观察可以跨过批准边界，保留实际响应时间。
    for _ in range(2):
        if observed_at >= approved_at:
            break
        time.sleep(1.1)
        observed_at = c.observe_order_failure()
    require(observed_at >= approved_at, "conditions", "observation_predates_approval")
    text = f"脚本工程师观察本地版本 {state['version']}，修订 {state['revision']}，订单 HTTP 500。"
    flow.post(
        conditions_path,
        dict(
            **flow.version(),
            decision_id=approved["id"],
            decision_content_sha256=approved["content_sha256"],
            expected_condition_version=conditions["condition_version"],
            previous_content_sha256=None,
            items=[
                dict(
                    condition_id=conditions["catalog"][0]["condition_id"],
                    status="verified",
                    explanation=text,
                    sources=[
                        dict(
                            kind="human_observation",
                            text=text,
                            observed_at=observed_at.isoformat(),
                        )
                    ],
                )
            ],
        ),
    )
    latest = c.call(conditions_path)["latest"]
    require(latest["content"]["status"] == "ready_for_review", "conditions", "not_ready_for_review")
    version_path = conditions_path + "/versions/" + str(latest["version"])
    approval_path = version_path + "/approval-request"
    flow.post(
        approval_path,
        dict(
            **flow.version(),
            verification_id=latest["id"],
            verification_content_sha256=latest["content_sha256"],
        ),
    )
    request = c.call(approval_path)["record"]
    flow.post(
        approval_path + "/decision",
        dict(
            action="approve",
            **flow.version(),
            request_id=request["id"],
            request_content_sha256=request["content_sha256"],
            acknowledge_verification=True,
        ),
        role="approver",
    )
    flow.emit(
        "approved",
        engineer_approval_rejected=True,
        revision_before=state["revision"],
        observation_time=observed_at.isoformat(),
        verification_id=latest["id"],
    )
    return version_path


def execute_and_converge(
    flow: Investigation, version_path: str, control: ComposeControl, *, failure: bool
) -> dict:
    c = flow.client
    before = c.call("/__admin/simulated-release", origin=ORDER)
    sampling_stopped = False
    try:
        if failure:
            # 停止命令结果未知时，容器也可能已停；仍须在退出时尝试恢复。
            sampling_stopped = True
            control.sampling(False)
        decision = c.call(version_path + "/approval-request/decision")["record"]
        release_path = version_path + "/release-execution-request"
        preparation = flow.post(
            release_path,
            dict(
                **flow.version(),
                approval_decision_id=decision["id"],
                approval_content_sha256=decision["content_sha256"],
            ),
            role="executor",
        )["request"]
        prepared = preparation["record"]
        require(prepared is not None, "execution", "release_preparation_missing")
        flow.emit(
            "release_prepared",
            request_id=prepared["id"],
            availability=preparation["availability"],
            created_at=prepared["created_at"],
            expires_at=prepared["expires_at"],
        )
        require(preparation["availability"] == "current", "execution", "release_preparation_stale")
        job_path = release_path + "/execution-job"
        body = dict(
            expected_incident_version=prepared["incident_version"],
            request_id=prepared["id"],
            request_content_sha256=prepared["content_sha256"],
        )
        created = flow.post(job_path, body, role="executor")
        replay = flow.post(job_path, body, role="executor")
        require(
            created["applied"]
            and not replay["applied"]
            and created["job"]["record"]["id"] == replay["job"]["record"]["id"],
            "execution",
            "idempotency_mismatch",
        )
        execution = c.poll(
            "execution", lambda: c.call(job_path), lambda value: value["result"] is not None
        )
        require(
            execution["result"]["outcome"] == "applied_current",
            "execution",
            execution["result"]["reason_code"],
        )
        after = c.call("/__admin/simulated-release", origin=ORDER)
        require(
            after["version"] == "stable" and after["revision"] == before["revision"] + 1,
            "execution",
            "not_exactly_one_rollback",
        )
        flow.emit(
            "executed",
            job_id=execution["record"]["id"],
            operation_id=execution["record"]["operation_id"],
            attempts=[dict(id=item["id"], status=item["status"]) for item in execution["attempts"]],
            duplicate_reused=True,
            revision_before=before["revision"],
            revision_after=after["revision"],
        )
        if failure:
            c.call(
                "/__admin/configuration/database-pool",
                dict(value=1, expected_value=50),
                method="PUT",
                origin=ORDER,
            )
    finally:
        if sampling_stopped:
            control.sampling(True)
    recovery = c.poll(
        "recovery",
        lambda: c.call(job_path + "/recovery-verification"),
        lambda value: value["result"] is not None,
    )
    selected = [
        item
        for item in recovery["samples"]
        if item["attempt_id"] == recovery["result"]["attempt_id"]
    ]
    statuses = [
        item["observation"]["http_status"] if item["observation"] else item["error_code"]
        for item in selected
    ]
    expected = "not_recovered" if failure else "recovered"
    require(
        recovery["result"]["outcome"] == expected
        and statuses == ([500] * 3 if failure else [200] * 3),
        "recovery",
        "objective_samples_did_not_match",
    )
    require(
        [item["sequence"] for item in selected] == [1, 2, 3], "recovery", "sample_sequence_mismatch"
    )
    convergence = c.poll(
        "convergence",
        lambda: c.call(flow.base + "/incident-convergence"),
        lambda value: value["record"] is not None,
    )["record"]
    expected_convergence = "observed_failure" if failure else "recovered"
    require(
        convergence["outcome"] == expected_convergence, "convergence", convergence["reason_code"]
    )
    incident = c.call(flow.incident)
    run = c.call(flow.base)
    require(
        incident["status"] == ("investigating" if failure else "resolved"),
        "convergence",
        "incident_status_mismatch",
    )
    require(
        convergence["slot_released"] is (not failure), "convergence", "slot_projection_mismatch"
    )
    require(run["status"] == "settled", "convergence", "run_not_settled")
    flow.emit(
        "recovery",
        attempt_id=recovery["result"]["attempt_id"],
        convergence_sha256=convergence["content_sha256"],
        samples=statuses,
        outcome=expected,
        incident_status=incident["status"],
        slot_released=convergence["slot_released"],
    )
    return convergence
