"""摘要上下文测试使用真实 SDK 序列化与持久化查询核对请求。"""

import json
from collections import defaultdict
from uuid import UUID

from opsswarm.investigation.budget.queries import CallBudgetQueries
from opsswarm.investigation.models import EvidenceObservation, InvestigationRequest
from opsswarm.investigation.tasks.queries import CollaborationQueries
from opsswarm.investigation.working_summary.contracts import WorkingSummaryRecord
from tests.checkpoint_fakes import ConclusionSynthesizerStub, CountingDataSource
from tests.loop_helpers import tool_response
from tests.model_budget_helpers import sdk_response


class CounterEvidenceSource(CountingDataSource):
    def query_logs(self, **kwargs):
        evidence = list(super().query_logs(**kwargs))
        evidence[0] = evidence[0].model_copy(
            update={"attributes": {**evidence[0].attributes, "counter_evidence": "仍有健康实例"}}
        )
        evidence.append(
            evidence[0].model_copy(
                update={"evidence_id": "unread-log-detail", "summary": "未读材料"}
            )
        )
        return evidence


class SummaryReplies:
    def __init__(self, db):
        self.calls = CallBudgetQueries(db)
        self.facts = CollaborationQueries(db)
        self.seen = defaultdict(list)
        self.synthesis = {}

    async def __call__(self, request):
        payload = json.loads(request.content)
        data = json.loads(payload["messages"][1]["content"])
        run_id = UUID(data["task_context"]["investigation_run_id"])
        role = data.get("specialist", "synthesis")
        self.seen[run_id].append(role)
        snapshot = await self.calls.get_snapshot(run_id)
        operation = "synthesize" if role == "synthesis" else f"analyze_{role}"
        call = next(
            c for c in snapshot.calls if c.status == "reserved" and c.operation == operation
        )
        manifest = await self.calls.get_context_manifest(run_id, call.id)
        assert manifest.version == "summary-context-v1"
        assert manifest.investigation_run_id == run_id
        if role == "synthesis":
            assert "reports" not in data and len(data["working_summaries"]) == 3
            assert manifest.prompt_version == "summary-context-v1" and manifest.tool_names == ()
            records = [WorkingSummaryRecord.model_validate(s) for s in data["working_summaries"]]
            references = {
                m.material_id: m for m in manifest.materials if m.kind == "task_working_summary"
            }
            assert len(references) == 3 and len(manifest.materials) == 10
            evidence = []
            for record in records:
                assert await self.facts.get_working_summary(run_id, record.task_id) == record
                assert references[record.task_id].content_sha256 == record.content_sha256
                assert references[record.task_id].version == record.version
                assert references[record.task_id].evidence_ids == record.summary.read_evidence_ids
                assert record.summary.report.findings == (
                    "反证：仍有健康实例",
                    "未解：异常是否持续",
                )
                assert len(record.summary.read_evidence_ids) == 1
                assert (
                    record.summary.read_evidence_ids[0]
                    == record.summary.evidence_catalog.entries[0].evidence_id
                )
                saved = await self.facts.get_evidence(run_id, record.task_id)
                evidence.extend(saved.snapshot.evidence)
            actual = [EvidenceObservation.model_validate(e) for e in data["evidence"]]
            assert sorted(actual, key=lambda e: e.evidence_id) == sorted(
                evidence, key=lambda e: e.evidence_id
            )
            self.synthesis[run_id] = data
            result = await ConclusionSynthesizerStub().synthesize(
                request=InvestigationRequest.model_validate(data["incident"]),
                evidence=actual,
                reports=[r.summary.report for r in records],
            )
            return sdk_response(payload, result.model_dump(mode="json"))
        assert manifest.prompt_version == "evidence-read-v1"
        assert manifest.tool_names == ("read_evidence",)
        catalog = data["evidence_catalog"]
        assert UUID(catalog["task_id"]) == manifest.task_id and catalog["role"] == role
        assert all("attributes" not in entry for entry in catalog["entries"])
        if len(payload["messages"]) == 2:
            return tool_response(
                "read_evidence",
                {
                    "evidence_id": catalog["entries"][0]["evidence_id"],
                    "snapshot_sha256": catalog["snapshot_sha256"],
                },
                call_id=f"{role}-read",
            )
        detail = json.loads(payload["messages"][-1]["content"])
        assert UUID(detail["investigation_run_id"]) == run_id
        assert detail["task_id"] == catalog["task_id"]
        return tool_response(
            "SpecialistReport",
            {
                "specialist": role,
                "summary": "完成有界调查",
                "findings": ["反证：仍有健康实例", "未解：异常是否持续"],
                "evidence_ids": [detail["evidence"]["evidence_id"]],
                "confidence": 0.8,
            },
            call_id=f"{role}-report",
        )
