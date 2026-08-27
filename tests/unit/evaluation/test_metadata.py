"""评测实验身份与运行遥测的纯领域校验。"""

from decimal import Decimal

import pytest
from pydantic import ValidationError

from opsswarm.evaluation.metadata import (
    EvaluationExperimentSpec,
    EvaluationKind,
    EvaluationModeTelemetry,
    empty_evaluation_telemetry,
    fixture_experiment_spec,
)


def test_fixture_experiment_cannot_claim_real_model_identity() -> None:
    with pytest.raises(ValidationError, match="冻结契约评测不能声明真实模型身份"):
        EvaluationExperimentSpec(
            experiment_key="fixture-contract-group",
            evaluation_kind=EvaluationKind.FIXTURE_CONTRACT,
            model_provider="openai-compatible",
            model_name="example-model",
            model_parameters_hash="a" * 64,
            single_agent_prompt_version="single-agent-v1",
            multi_agent_prompt_version="multi-agent-parallel-v1",
        )


def test_real_model_experiment_requires_complete_identity() -> None:
    with pytest.raises(ValidationError, match="真实模型评测必须声明"):
        EvaluationExperimentSpec(
            experiment_key="real-model-group",
            evaluation_kind=EvaluationKind.REAL_MODEL,
            model_provider="openai-compatible",
            model_name="example-model",
            single_agent_prompt_version="single-agent-v1",
            multi_agent_prompt_version="multi-agent-parallel-v1",
        )

    experiment = EvaluationExperimentSpec(
        experiment_key="real-model-group",
        evaluation_kind=EvaluationKind.REAL_MODEL,
        model_provider="openai-compatible",
        model_name="example-model",
        model_parameters_hash="b" * 64,
        single_agent_prompt_version="single-agent-v1",
        multi_agent_prompt_version="multi-agent-parallel-v1",
    )

    assert experiment.model_name == "example-model"


def test_mode_telemetry_requires_recomputable_token_total() -> None:
    with pytest.raises(ValidationError, match="total_tokens 必须等于"):
        EvaluationModeTelemetry(
            input_tokens=10,
            output_tokens=5,
            total_tokens=14,
        )

    telemetry = EvaluationModeTelemetry(
        input_tokens=10,
        output_tokens=5,
        total_tokens=15,
        estimated_cost_usd=Decimal("0.000125"),
    )

    assert telemetry.total_tokens == 15
    assert telemetry.estimated_cost_usd == Decimal("0.000125")


def test_fixture_helpers_produce_explicit_zero_telemetry() -> None:
    experiment = fixture_experiment_spec("fixture-helper-group")
    telemetry = empty_evaluation_telemetry()

    assert experiment.evaluation_kind is EvaluationKind.FIXTURE_CONTRACT
    assert telemetry.single_agent.total_tokens == 0
    assert telemetry.multi_agent.failures.total == 0
