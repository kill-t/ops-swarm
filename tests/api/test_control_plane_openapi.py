"""控制面 OpenAPI 契约测试。"""

from opsswarm.control_plane.app import create_app


def test_openapi_exposes_current_incident_contract() -> None:
    schema = create_app().openapi()
    paths = schema["paths"]

    assert set(paths) >= {
        "/health",
        "/api/v1/alerts",
        "/api/v1/incidents",
        "/api/v1/incidents/{incident_id}",
        "/api/v1/incidents/{incident_id}/actions/start-investigation",
        "/api/v1/incidents/{incident_id}/events",
        "/api/v1/investigations/{run_id}",
    }
    assert set(paths["/api/v1/alerts"]["post"]["responses"]) >= {"200", "201", "422"}
    validation_schema = paths["/api/v1/incidents"]["get"]["responses"]["422"]["content"][
        "application/json"
    ]["schema"]
    assert validation_schema["$ref"].endswith("/ApiErrorResponse")
