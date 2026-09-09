import pytest
from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    SystemMessage,
)
from langchain_core.messages import ToolMessage as LangChainToolMessage
from pydantic import ValidationError

from opsswarm.investigation.budget.model_contracts import TextModelMessage
from opsswarm.investigation.loop.contracts import AssistantMessage, ToolCall, ToolMessage
from opsswarm.investigation.loop.messages import (
    from_langchain,
    model_result,
    reported_usage,
    to_langchain,
    validate_history,
)

SNAPSHOT_SHA256 = "a" * 64
READ_ARGS = {"evidence_id": "log-1", "snapshot_sha256": SNAPSHOT_SHA256}
REPORT_ARGS = {
    "specialist": "log",
    "summary": "日志异常与发布窗口一致。",
    "findings": ["发现错误率显著上升。"],
    "evidence_ids": ["log-1"],
    "confidence": 0.9,
}


def _ai_call(name: str, args: dict[str, object], call_id: str) -> AIMessage:
    return AIMessage(
        content="  保留空白  ",
        tool_calls=[{"name": name, "args": args, "id": call_id, "type": "tool_call"}],
    )


def test_round_trip_preserves_text_and_complete_tool_pair() -> None:
    source = [
        SystemMessage(content=" system\n"),
        HumanMessage(content=" user "),
        _ai_call("read_evidence", READ_ARGS, "call-1"),
        LangChainToolMessage(content=" evidence ", tool_call_id="call-1", name="read_evidence"),
    ]

    normalized = from_langchain(source)
    restored = to_langchain(normalized)

    assert [message.content for message in restored] == [message.content for message in source]
    assert isinstance(restored[2], AIMessage)
    assert restored[2].tool_calls[0]["args"] == READ_ARGS
    assert isinstance(restored[3], LangChainToolMessage)
    assert restored[3].tool_call_id == "call-1"


def test_model_result_accepts_read_request_and_final_output_pair() -> None:
    request = model_result([_ai_call("read_evidence", READ_ARGS, "read-1")])
    final = model_result(
        [
            _ai_call("SpecialistReport", REPORT_ARGS, "report-1"),
            LangChainToolMessage(
                content="Returning structured response",
                tool_call_id="report-1",
                name="SpecialistReport",
            ),
        ]
    )

    assert request.messages[0].tool_calls[0].name == "read_evidence"
    assert len(final.messages) == 2
    assert final.messages[0].tool_calls[0].args["specialist"] == "log"


@pytest.mark.parametrize(
    "messages, expected",
    [
        ([_ai_call("unknown", {}, "bad")], "不允许的工具"),
        ([AIMessage(content="plain")], "必须且只能包含一个工具调用"),
        (
            [
                AIMessage(
                    content="",
                    invalid_tool_calls=[
                        {"name": "read_evidence", "args": "{", "id": "bad", "error": "bad"}
                    ],
                )
            ],
            "无法解析",
        ),
        (
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "read_evidence",
                            "args": READ_ARGS,
                            "id": "one",
                            "type": "tool_call",
                        },
                        {
                            "name": "read_evidence",
                            "args": READ_ARGS,
                            "id": "two",
                            "type": "tool_call",
                        },
                    ],
                ),
            ],
            "必须且只能",
        ),
    ],
)
def test_model_result_rejects_illegal_responses(messages: list[AIMessage], expected: str) -> None:
    with pytest.raises((ValueError, ValidationError), match=expected):
        model_result(messages)


def test_history_rejects_duplicate_id_and_mismatched_or_missing_pair() -> None:
    prefix = (
        TextModelMessage(role="system", content="system"),
        TextModelMessage(role="user", content="user"),
    )
    assistant = AssistantMessage(
        content="", tool_calls=(ToolCall(id="same", name="read_evidence", args=READ_ARGS),)
    )
    paired = ToolMessage(content="detail", tool_call_id="same", name="read_evidence")

    with pytest.raises(ValueError, match="缺少配对"):
        validate_history((*prefix, assistant))
    with pytest.raises(ValueError, match="不匹配"):
        validate_history(
            (
                *prefix,
                assistant,
                ToolMessage(content="detail", tool_call_id="other", name="read_evidence"),
            )
        )
    with pytest.raises(ValueError, match="不能重复"):
        validate_history((*prefix, assistant, paired, assistant, paired))


def test_from_langchain_rejects_non_text_and_error_tool_message() -> None:
    with pytest.raises(ValueError, match="只接受文本"):
        from_langchain(
            [
                SystemMessage(content="system"),
                HumanMessage(content=[{"type": "text", "text": "user"}]),
            ]
        )
    with pytest.raises(ValueError, match="失败工具响应"):
        from_langchain(
            [
                LangChainToolMessage(
                    content="failed",
                    tool_call_id="read-1",
                    name="read_evidence",
                    status="error",
                )
            ]
        )


def test_tool_arguments_are_validated_by_contract() -> None:
    with pytest.raises(ValidationError):
        model_result(
            [
                _ai_call(
                    "read_evidence",
                    {"evidence_id": "log-1", "snapshot_sha256": SNAPSHOT_SHA256, "extra": 1},
                    "read-1",
                )
            ]
        )


def test_from_langchain_leaves_full_history_validation_to_explicit_boundary() -> None:
    normalized = from_langchain([_ai_call("read_evidence", READ_ARGS, "read-1")])

    assert isinstance(normalized[0], AssistantMessage)
    with pytest.raises(ValueError, match="至少需要"):
        validate_history(normalized)


def test_reported_usage_requires_complete_provider_raw_counts() -> None:
    provider_over_compatibility_fields = AIMessage(
        content="",
        usage_metadata={"input_tokens": 10, "output_tokens": 4, "total_tokens": 14},
        response_metadata={
            "token_usage": {"prompt_tokens": 99, "completion_tokens": 1, "total_tokens": 100}
        },
    )
    provider = AIMessage(
        content="",
        response_metadata={
            "token_usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}
        },
    )

    assert reported_usage(provider_over_compatibility_fields).model_dump() == {
        "input_tokens": 99,
        "output_tokens": 1,
        "total_tokens": 100,
    }
    assert reported_usage(provider).total_tokens == 10
    assert reported_usage(AIMessage(content="")) is None
    assert (
        reported_usage(
            AIMessage(
                content="",
                usage_metadata={"input_tokens": 1, "output_tokens": 0, "total_tokens": 1},
                response_metadata={"token_usage": {"prompt_tokens": 1}},
            )
        )
        is None
    )
    assert (
        reported_usage(
            AIMessage(
                content="",
                usage_metadata={"input_tokens": 1, "output_tokens": 2, "total_tokens": 3},
            )
        )
        is None
    )
    with pytest.raises(ValidationError):
        reported_usage(
            AIMessage(
                content="",
                response_metadata={
                    "token_usage": {
                        "prompt_tokens": 1,
                        "completion_tokens": 2,
                        "total_tokens": 4,
                    }
                },
            )
        )
