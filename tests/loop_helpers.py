"""证据循环的真实 SDK 假响应与独立专业尝试准备。"""

import json

import httpx2

from opsswarm.investigation.budget.contracts import CallOperation
from opsswarm.investigation.budget.store import CallBudgetStore
from opsswarm.investigation.loop.service import EvidenceLoopService
from tests.model_budget_helpers import model_policy
from tests.task_evidence_helpers import evidence_attempt, save_evidence


def loop_policy(**changes):
    profile = model_policy().profile.model_copy(
        update={"structured_output_method": "function_calling"}
    )
    return model_policy(profile=profile, max_output_tokens_per_call=1024, **changes)


def tool_response(name, arguments, *, call_id="tool-1", usage=True):
    body = {
        "id": "fake-completion",
        "object": "chat.completion",
        "created": 0,
        "model": "fake-budget",
        "choices": [
            {
                "index": 0,
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": name,
                                "arguments": json.dumps(arguments, ensure_ascii=False),
                            },
                        }
                    ],
                },
            }
        ],
    }
    if usage:
        body["usage"] = {"prompt_tokens": 80, "completion_tokens": 20, "total_tokens": 100}
    return httpx2.Response(200, json=body)


async def prepared_loop(db, *, policy=None, call_budget=None, build_packet=True):
    policy = policy or loop_policy()
    prepared = await evidence_attempt(
        db,
        model_budget=policy,
        context_policy_version="evidence-read-v1",
        call_budget=call_budget,
    )
    await save_evidence(prepared)
    _, claim, clock, _, task, attempt, _ = prepared
    service = EvidenceLoopService(db, claim.run.id)
    packet = (
        await service.prepare(task.id, attempt.id, CallOperation.ANALYZE_LOG, 0, policy)
        if build_packet
        else None
    )
    return prepared, packet, CallBudgetStore(db, clock=clock), service


async def reserve_loop(prepared, packet, store, index=0):
    _, claim, _, _, task, attempt, _ = prepared
    return await store.reserve(
        claim.run.id,
        lease_token=claim.work_item.lease_token,
        task_id=task.id,
        task_attempt_id=attempt.id,
        operation=CallOperation.ANALYZE_LOG,
        model_input=packet.model_input,
        context_manifest=packet.manifest,
        prompt_version=packet.manifest.prompt_version,
        invocation_index=index,
    )
