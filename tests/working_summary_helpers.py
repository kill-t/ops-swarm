"""工作摘要测试通过真实工具循环建立合法完成材料。"""

import json

import httpx2

from opsswarm.investigation.budget.runtime import BudgetedCallRunner
from opsswarm.investigation.loop.agent import EvidenceLoopAnalyst
from opsswarm.investigation.models import EvidenceKind
from opsswarm.investigation.tasks.contracts import TaskOutcome
from tests.loop_helpers import loop_policy, prepared_loop, tool_response
from tests.model_budget_helpers import sdk_model
from tests.task_evidence_helpers import material, professional_result


async def analyzed_task(db, *, prepared=None, store=None, service=None):
    if prepared is None:
        prepared, _, store, service = await prepared_loop(db)
    _, claim, _, tasks, task, attempt, _ = prepared
    sent = 0

    async def reply(request):
        nonlocal sent
        sent += 1
        catalog = json.loads(request.content)["messages"][1]["content"]
        catalog = json.loads(catalog)["evidence_catalog"]
        if sent == 1:
            return tool_response(
                "read_evidence",
                {
                    "evidence_id": catalog["entries"][0]["evidence_id"],
                    "snapshot_sha256": catalog["snapshot_sha256"],
                },
            )
        return tool_response(
            "SpecialistReport",
            {
                "specialist": "log",
                "summary": "调查完成但判断仍有限",
                "findings": ["反证：存在正常请求", "未解问题：其他节点是否一致"],
                "evidence_ids": [catalog["entries"][0]["evidence_id"]],
                "confidence": 0.6,
            },
            call_id="final-report",
        )

    runner = BudgetedCallRunner(
        run_id=claim.run.id, lease_token=claim.work_item.lease_token, store=store
    )
    async with httpx2.AsyncClient(transport=httpx2.MockTransport(reply)) as client:
        analyst = EvidenceLoopAnalyst(
            sdk_model(client), policy=loop_policy(), runner=runner, service=service
        )
        with runner.task_scope(task.id, attempt.id):
            report = await analyst.analyze(
                specialist=EvidenceKind.LOG, request=claim.request, evidence=material()
            )
    result = professional_result(task.assignment).model_copy(update={"report": report})
    return prepared, result


async def finish(prepared, result):
    _, claim, _, tasks, task, attempt, _ = prepared
    return await tasks.finish_attempt(
        claim.run.id,
        task.id,
        attempt.id,
        lease_token=claim.work_item.lease_token,
        outcome=TaskOutcome(status="succeeded", summary=result.report.summary),
        result=result,
    )
