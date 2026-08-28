"""真实模型冒烟命令的显式身份参数测试。"""

import pytest

from opsswarm.evaluation.real_main import RealModelCommandOptions, parse_arguments


def test_real_model_command_requires_experiment_and_idempotency_identity() -> None:
    with pytest.raises(SystemExit):
        parse_arguments([])


def test_real_model_command_uses_single_scenario_defaults() -> None:
    options = parse_arguments(
        [
            "--experiment-key",
            "real-model-smoke-exp01",
            "--idempotency-prefix",
            "real-model-smoke-exp01-r01",
        ]
    )

    assert options == RealModelCommandOptions(
        experiment_key="real-model-smoke-exp01",
        repetition_index=1,
        scenario_id="release-500-baseline-v1",
        idempotency_prefix="real-model-smoke-exp01-r01",
    )


def test_real_model_command_rejects_out_of_range_repetition() -> None:
    with pytest.raises(SystemExit):
        parse_arguments(
            [
                "--experiment-key",
                "real-model-smoke-exp01",
                "--idempotency-prefix",
                "real-model-smoke-exp01-r1001",
                "--repetition-index",
                "1001",
            ]
        )
