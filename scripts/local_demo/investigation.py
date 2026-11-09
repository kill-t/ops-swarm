"""通过现有控制面创建现场演练并实际续接人工等待。"""

import time
from uuid import UUID, uuid5

from scripts.local_demo.client import DemoClient, require
from scripts.local_demo.config import CHANGE, ORDER, model_budget

REASON = "本地脚本模型工程链演示"


class Investigation:
    def __init__(self, client: DemoClient, case_id: str, emit) -> None:
        self.client, self.case_id, self.emit = client, case_id, emit
        self.base = self.incident = ""

    def post(self, path, body, *, role="engineer", status=None, suffix=""):
        key = str(uuid5(UUID(self.case_id), f"{role}:{path}:{suffix}"))
        return self.client.call(
            path,
            {
                "reason": REASON,
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
        # 开始调查以事故版本控制并发，接口只接收其声明的命令字段。
        run = c.call(
            self.incident + "/actions/start-investigation",
            dict(
                expected_version=1,
                reason=REASON,
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
        wait = c.call(self.base + "/human-wait")
        require(wait["status"] == "waiting", "investigation", "handoff_not_waiting")
        rounds = c.call(self.base + "/collaboration")["rounds"]
        completed = [item for item in rounds if item["status"] == "completed"]
        roles = {
            task["assignment"]["role"]
            for item in completed
            for task in item["tasks"]
            if task["status"] == "succeeded"
        }
        require({"log", "metric", "change"} <= roles, "investigation", "professional_tasks_missing")
        budget = c.call(self.base + "/call-budget")
        self.emit(
            "handoff",
            run_id=run,
            status=state["status"],
            handoff_id=wait["handoff_id"],
            wait_version=wait["state_version"],
            completed_rounds=[item["round_number"] for item in completed],
            completed_roles=sorted(roles),
            model_calls=budget["reserved_model_calls"],
            deadline=budget["deadline"],
        )
        return budget

    def continue_human(self) -> str:
        c = self.client
        wait_path = self.base + "/human-wait"

        def basis(wait):
            return dict(
                handoff_id=wait["handoff_id"],
                source_sha256=wait["source_sha256"],
                expected_wait_version=wait["state_version"],
            )

        before = c.call(wait_path)
        saved = self.post(
            wait_path + "/supplements",
            dict(
                action="supplement",
                **basis(before),
                items=[
                    dict(
                        kind="statement", text="脚本演示工程师已核对本地发布状态与订单 HTTP 失败。"
                    )
                ],
            ),
        )
        wait = c.call(wait_path)
        require(
            saved["applied"]
            and wait["status"] == "waiting"
            and wait["handoff_id"] == before["handoff_id"]
            and wait["source_sha256"] == before["source_sha256"]
            and wait["state_version"] == before["state_version"] + 1
            and bool(wait["events"])
            and wait["events"][-1] == saved["event"],
            "human_supplement",
            "supplement_not_persisted",
        )
        bound = basis(wait)
        preparation = self.post(wait_path + "/preparations", dict(action="prepare", **bound))
        prepare_path = wait_path + "/preparations/" + preparation["job"]["id"]
        prepared = self.wait_job("human_preparation", prepare_path)
        require(
            prepared["availability"] == "ready"
            and prepared["wait_version"] == wait["state_version"],
            "human_preparation",
            "source_not_ready",
        )
        reassessment = self.post(
            prepare_path + "/reassessments",
            dict(action="reassess", **bound, preparation_sha256=prepared["artifact_sha256"]),
        )
        review_id = reassessment["job"]["id"]
        review_path = prepare_path + "/reassessments/" + review_id
        self.wait_job("human_review", review_path)
        routed = c.poll(
            "human_route",
            lambda: c.call(review_path + "/route"),
            lambda value: value["record"] is not None,
        )
        route = routed["record"]
        require(
            routed["availability"] == "current"
            and route["decision"]["route"] == "candidate_evaluation"
            and route["wait_version"] == wait["state_version"],
            "human_route",
            "unsupported_direction",
        )
        nav = self.nav()
        require(
            nav["review_job_id"] == review_id and bool(nav["review_route_sha256"]),
            "human_route",
            "review_basis_changed",
        )
        candidates = self.base + "/candidate-evaluations"
        evaluation = self.post(
            candidates,
            dict(
                action="evaluate_candidate",
                source_kind="human_route",
                source_id=route["id"],
                source_sha256=nav["review_route_sha256"],
                expected_wait_version=route["wait_version"],
            ),
        )
        candidate_id = evaluation["job"]["id"]
        candidate_path = candidates + "/" + candidate_id
        candidate = self.wait_job("candidate", candidate_path)
        require(
            candidate["availability"] == "current"
            and candidate["wait_version"] == wait["state_version"]
            and candidate["result"]["content"]["disposition"] == "planning_review",
            "candidate",
            "not_plannable",
        )
        self.emit(
            "human_continued",
            supplement_id=saved["event"]["id"],
            wait_version=wait["state_version"],
            preparation_id=preparation["job"]["id"],
            review_job_id=review_id,
            review_route_id=route["id"],
            candidate_id=candidate_id,
        )
        return candidate_path
