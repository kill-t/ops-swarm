"""启动入口只在显式满足能力时开启按需证据读取。"""

import pytest
from pydantic import ValidationError

from opsswarm.control_plane.schemas import StartInvestigationRequest
from opsswarm.investigation.context.selection import (
    resolve_context_policy,
    select_context_prompt,
)
from opsswarm.investigation.workflow_versions import WorkflowVersion
from tests.model_budget_helpers import model_policy


def budget(method: str):
    original = model_policy()
    return original.model_copy(
        update={"profile": original.profile.model_copy(update={"structured_output_method": method})}
    )


def request(**changes):
    return StartInvestigationRequest.model_validate(
        {
            "expected_version": 1,
            "actor_id": "engineer",
            "reason": "验证上下文策略",
            "mode": "multi_agent",
            "workflow_version": "v2",
            **changes,
        }
    )


def test_omitted_policy_preserves_existing_selection():
    assert request().context_policy_version is None
    assert resolve_context_policy(WorkflowVersion.V2, None, None) is None
    assert resolve_context_policy(WorkflowVersion.V2, model_policy(), None) == "task-context-v1"
    assert select_context_prompt(None, "multi-agent-parallel-v2") == "multi-agent-parallel-v2"
    assert select_context_prompt("task-context-v1", "unused") == "fixed-task-context-v1"


def test_explicit_fixed_policy_requires_v2_and_model_budget():
    assert (
        request(
            model_budget=model_policy(), context_policy_version="task-context-v1"
        ).context_policy_version
        == "task-context-v1"
    )
    with pytest.raises(ValidationError, match="必须配置模型预算"):
        request(context_policy_version="task-context-v1")
    with pytest.raises(ValidationError, match="只有 v2"):
        request(
            workflow_version="v1",
            model_budget=model_policy(),
            context_policy_version="task-context-v1",
        )


def test_loop_policy_requires_function_calling_profile():
    assert (
        request(
            model_budget=budget("function_calling"), context_policy_version="evidence-read-v1"
        ).context_policy_version
        == "evidence-read-v1"
    )
    assert select_context_prompt("evidence-read-v1", "unused") == "evidence-read-v1"
    with pytest.raises(ValidationError, match="function_calling"):
        request(model_budget=model_policy(), context_policy_version="evidence-read-v1")


def test_explicit_null_is_rejected_but_omission_remains_compatible():
    assert request(model_budget=model_policy()).context_policy_version is None
    with pytest.raises(ValidationError, match="不能显式设为 null"):
        request(model_budget=model_policy(), context_policy_version=None)
