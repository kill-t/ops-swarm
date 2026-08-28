"""真实模型三场景批量命令参数测试。"""

import pytest

from opsswarm.evaluation import MVP_INVESTIGATION_DATASET_V1
from opsswarm.evaluation.real_batch_main import (
    RealModelBatchCommandOptions,
    parse_arguments,
)


def test_batch_command_defaults_to_all_scenarios_once() -> None:
    options = parse_arguments(
        [
            "--experiment-key",
            "real-batch-exp01",
            "--idempotency-prefix",
            "real-batch-exp01",
        ]
    )

    assert options == RealModelBatchCommandOptions(
        experiment_key="real-batch-exp01",
        idempotency_prefix="real-batch-exp01",
        repetition_start=1,
        repetitions=1,
        scenario_ids=tuple(item.scenario_id for item in MVP_INVESTIGATION_DATASET_V1.scenarios),
    )
    assert options.repetition_indexes == (1,)


def test_batch_command_accepts_explicit_scenario_and_repetition_range() -> None:
    options = parse_arguments(
        [
            "--experiment-key",
            "real-batch-exp01",
            "--idempotency-prefix",
            "real-batch-exp01",
            "--repetition-start",
            "2",
            "--repetitions",
            "3",
            "--scenario-id",
            "redis-unavailable-v1",
        ]
    )

    assert options.repetition_indexes == (2, 3, 4)
    assert options.scenario_ids == ("redis-unavailable-v1",)


@pytest.mark.parametrize(
    "extra_arguments",
    [
        ["--repetitions", "11"],
        ["--repetition-start", "1000", "--repetitions", "2"],
        [
            "--scenario-id",
            "redis-unavailable-v1",
            "--scenario-id",
            "redis-unavailable-v1",
        ],
    ],
)
def test_batch_command_rejects_unsafe_plan(extra_arguments: list[str]) -> None:
    with pytest.raises(SystemExit):
        parse_arguments(
            [
                "--experiment-key",
                "real-batch-exp01",
                "--idempotency-prefix",
                "real-batch-exp01",
                *extra_arguments,
            ]
        )
