"""通过现有控制面创建现场演练并实际续接人工等待。"""

import time
from uuid import UUID, uuid5

from scripts.local_demo.client import DemoClient, require
from scripts.local_demo.config import CHANGE, ORDER, model_budget


class Investigation:
    def __init__(self, client: DemoClient, case_id: str, emit) -> None:
        self.client, self.case_id, self.emit = client, case_id, emit
        self.base = self.incident = ""

    def post(self, path, body, *, role="engineer", status=None, suffix=""):
        key = str(uuid5(UUID(self.case_id), f"{role}:{path}:{suffix}"))
        return self.client.call(
            path,
            {
                "reason": "本地脚本模型工程链演示",
                "idempotency_key": key,
                **body,
            },
            role=role,
            status=status,
        )

    def version(self):
        return {"expected_incident_version": self.client.call(self.incident)["version"]}

    def nav(self):
        return self.client.call(self.incident + "/collaboration-navigation")

    def wait_job(self, stage, path):
        value = self.client.poll(
            stage,
            lambda: self.client.call(path),
            lambda value: value["status"] not in {"queued", "running"},
        )
        require(value["status"] == "succeeded", stage, "worker_did_not_succeed")
        return value

    def start(self) -> dict:
        c = self.client
        pool = c.call("/__admin/configuration/database-pool", origin=ORDER)
        if pool["value"] != 50:
            c.call(
                "/__admin/configuration/database-pool",
                dict(value=50, expected_value=pool["value"]),
                method="PUT",
                origin=ORDER,
            )
        state = c.call("/__admin/simulated-release", origin=ORDER)
        if state["version"] == "2026.01.01-rc1":
            c.call(
                "/__admin/simulated-release",
                dict(
                    expected_version=state["version"],
                    expected_revision=state["revision"],
                    target_version="stable",
                    idempotency_key="baseline-" + self.case_id,
                ),
                method="PUT",
                origin=ORDER,
            )
            state = c.call("/__admin/simulated-release", origin=ORDER)
        require(state["version"] == "stable", "start", "unexpected_lab_version")
        c.call(
            "/__admin/simulated-release",
            dict(
                expected_version="stable",
                expected_revision=state["revision"],
                target_version="2026.01.01-rc1",
                idempotency_key="fault-" + self.case_id,
            ),
            method="PUT",
            origin=ORDER,
        )
        c.call("/orders/local-demo", origin=ORDER, status=500)
        changed = c.call(
            "/__admin/changes",
            dict(
                change_type="deployment",
                external_change_id="deploy-" + self.case_id,
                from_version="stable",
                to_version="2026.01.01-rc1",
            ),
            origin=CHANGE,
        )
        # 截止按秒固定，等待变更落在截止以前，不修改服务时间或事件发生时间。
        time.sleep(1.1)
        incident = c.call(
            "/api/v1/alerts",
            dict(
                source="local-script-demo",
                external_event_id=self.case_id,
                fingerprint=self.case_id,
                service_name="order-service",
                severity="p1",
                title="本地多 Agent 工程链演示",
                occurred_at=changed["record"]["occurred_at"],
                payload={"scripted_model": True},
            ),
            role="alert_source",
        )["incident"]["id"]
        self.incident = f"/api/v1/incidents/{incident}"
        self.emit("incident", incident_id=incident)
        run = self.post(
            self.incident + "/actions/start-investigation",
            dict(
                expected_version=1,
                mode="multi_agent",
                data_source_mode="live",
                workflow_version="v3",
                model_budget=model_budget(),
                call_budget=dict(
                    max_model_calls=20,
                    max_tool_calls=18,
                    max_tool_calls_per_task=6,
                    max_duration_seconds=1800,
                ),
            ),
        )["investigation"]["id"]
        self.base = f"/api/v1/investigations/{run}"
        self.emit("investigation_started", run_id=run)
        state = c.poll(
            "investigation",
            lambda: c.call(self.base),
            lambda value: value["status"] not in {"queued", "running"},
        )
        require(state["status"] == "await_human", "investigation", "did_not_handoff")
        budget = c.call(self.base + "/call-budget")
        self.emit(
            "handoff",
            run_id=run,
            status=state["status"],
            model_calls=budget["reserved_model_calls"],
            deadline=budget["deadline"],
        )
        return budget

    def continue_human(self) -> str:
        c = self.client
        wait_path = self.base + "/human-wait"

        def basis():
            wait = c.call(wait_path)
            return dict(
                handoff_id=wait["handoff_id"],
                source_sha256=wait["source_sha256"],
                expected_wait_version=wait["state_version"],
            )

        self.post(
            wait_path + "/supplements",
            dict(
                action="supplement",
                **basis(),
                items=[
                    dict(
                        kind="statement", text="脚本演示工程师已核对本地发布状态与订单 HTTP 失败。"
                    )
                ],
            ),
        )
        self.post(wait_path + "/preparations", dict(action="prepare", **basis()))
        nav = self.nav()
        prepare_path = wait_path + "/preparations/" + nav["preparation_id"]
        prepared = self.wait_job("human_preparation", prepare_path)
        require(prepared["availability"] == "ready", "human_preparation", "source_not_ready")
        self.post(
            prepare_path + "/reassessments",
            dict(action="reassess", **basis(), preparation_sha256=prepared["artifact_sha256"]),
        )
        nav = self.nav()
        review_path = prepare_path + "/reassessments/" + nav["review_job_id"]
        self.wait_job("human_review", review_path)
        route = c.poll(
            "human_route",
            lambda: c.call(review_path + "/route"),
            lambda value: value["record"] is not None,
        )["record"]
        require(
            route["decision"]["route"] == "candidate_evaluation",
            "human_route",
            "unsupported_direction",
        )
        nav = self.nav()
        candidates = self.base + "/candidate-evaluations"
        self.post(
            candidates,
            dict(
                action="evaluate_candidate",
                source_kind="human_route",
                source_id=route["id"],
                source_sha256=nav["review_route_sha256"],
                expected_wait_version=route["wait_version"],
            ),
        )
        candidate_path = candidates + "/" + self.nav()["candidate_id"]
        candidate = self.wait_job("candidate", candidate_path)
        require(
            candidate["result"]["content"]["disposition"] == "planning_review",
            "candidate",
            "not_plannable",
        )
        self.emit(
            "human_continued",
            candidate_id=candidate["id"] if "id" in candidate else self.nav()["candidate_id"],
        )
        return candidate_path
