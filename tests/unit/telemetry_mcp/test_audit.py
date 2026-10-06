import pytest

from opsswarm.investigation.budget.runtime import _audit_error_code
from opsswarm.investigation.errors import InvestigationDataSourceFailure


@pytest.mark.parametrize(
    "error_code",
    [
        "telemetry_mcp_invalid_request",
        "telemetry_mcp_timeout",
        "telemetry_mcp_unavailable",
        "telemetry_mcp_protocol_error",
        "telemetry_mcp_invalid_result",
        "telemetry_mcp_tool_error",
    ],
)
def test_budget_audit_preserves_only_fixed_telemetry_mcp_errors(error_code: str) -> None:
    error = InvestigationDataSourceFailure(error_code=error_code, message="敏感来源正文")

    assert _audit_error_code(error) == error_code


def test_budget_audit_collapses_unknown_source_error() -> None:
    error = InvestigationDataSourceFailure(
        error_code="untrusted_source_detail",
        message="敏感来源正文",
    )

    assert _audit_error_code(error) == "operation_failed"
