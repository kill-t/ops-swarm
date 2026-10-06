from pathlib import Path

import yaml


def test_compose_isolates_telemetry_mcp_from_business_credentials_and_ports() -> None:
    compose = yaml.safe_load(Path("compose.yaml").read_text(encoding="utf-8"))
    service = compose["services"]["telemetry-mcp"]
    environment = service["environment"]

    assert service["networks"] == ["telemetry"]
    assert "ports" not in service
    assert set(environment) == {
        "OPSSWARM_INVESTIGATION_LOKI_BASE_URL",
        "OPSSWARM_INVESTIGATION_REQUEST_TIMEOUT_SECONDS",
        "OPSSWARM_TELEMETRY_MCP_HOST",
        "OPSSWARM_TELEMETRY_MCP_PORT",
    }
    assert not any(
        forbidden in key
        for key in environment
        for forbidden in ("DATABASE", "CHECKPOINT", "LLM", "GITHUB", "TOKEN", "SECRET")
    )
    assert service["read_only"] is True
    assert service["cap_drop"] == ["ALL"]
    assert service["security_opt"] == ["no-new-privileges:true"]

    worker = compose["services"]["investigation-worker-live"]
    assert worker["environment"]["OPSSWARM_INVESTIGATION_LOKI_BASE_URL"] == "http://loki:3100"
    assert worker["environment"]["OPSSWARM_TELEMETRY_MCP_ENDPOINT_URL"] == (
        "http://telemetry-mcp:8000/mcp"
    )
    assert "telemetry-mcp" not in worker["depends_on"]
    assert worker["depends_on"]["loki"]["condition"] == "service_healthy"
    assert "telemetry" in worker["networks"]
    assert compose["services"]["loki"]["networks"] == ["telemetry"]
    assert compose["services"]["alloy"]["networks"] == ["telemetry"]


def test_compose_isolates_metric_mcp_and_preserves_historical_paths() -> None:
    compose = yaml.safe_load(Path("compose.yaml").read_text(encoding="utf-8"))
    service = compose["services"]["telemetry-metrics-mcp"]
    environment = service["environment"]

    assert service["networks"] == ["telemetry"]
    assert "ports" not in service
    assert set(environment) == {
        "OPSSWARM_INVESTIGATION_PROMETHEUS_BASE_URL",
        "OPSSWARM_INVESTIGATION_REQUEST_TIMEOUT_SECONDS",
        "OPSSWARM_TELEMETRY_METRICS_MCP_HOST",
        "OPSSWARM_TELEMETRY_METRICS_MCP_PORT",
    }
    assert not any(
        forbidden in key
        for key in environment
        for forbidden in ("DATABASE", "CHECKPOINT", "LLM", "GITHUB", "TOKEN", "SECRET")
    )
    assert service["depends_on"] == {"prometheus": {"condition": "service_healthy"}}
    assert service["read_only"] is True
    assert service["cap_drop"] == ["ALL"]
    assert service["security_opt"] == ["no-new-privileges:true"]

    worker = compose["services"]["investigation-worker-live"]
    assert worker["environment"]["OPSSWARM_INVESTIGATION_PROMETHEUS_BASE_URL"] == (
        "http://prometheus:9090"
    )
    assert worker["environment"]["OPSSWARM_TELEMETRY_METRICS_MCP_ENDPOINT_URL"] == (
        "http://telemetry-metrics-mcp:8000/mcp"
    )
    assert "telemetry-metrics-mcp" not in worker["depends_on"]
    assert worker["depends_on"]["prometheus"]["condition"] == "service_healthy"
    assert compose["services"]["prometheus"]["networks"] == ["lab", "telemetry"]
