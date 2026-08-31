"""GitHub 只读变更数据源的固定请求、规范化与失败关闭测试。"""

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import httpx2
import pytest
from pydantic import SecretStr, ValidationError

from opsswarm.investigation.errors import (
    InvestigationDataSourceFailure,
    UnsupportedInvestigationTarget,
)
from opsswarm.investigation.github_changes import (
    GITHUB_API_VERSION,
    MAX_RESPONSE_BYTES,
    GitHubChangeOnlyInvestigationDataSource,
    GitHubChangeSettings,
    GitHubEnrichedChangeDataSource,
)
from opsswarm.investigation.models import EvidenceKind, EvidenceObservation

NOW = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)
NOW_TIMESTAMP = NOW.timestamp()
COMMIT_SHA = "a" * 40
MERGE_SHA = "b" * 40


def _commit(*, html_url: str | None = None) -> dict[str, object]:
    return {
        "sha": COMMIT_SHA,
        "html_url": html_url or f"https://github.com/kill-t/ops-swarm/commit/{COMMIT_SHA}",
        "commit": {
            "message": "修复订单接口\u0000\n\n这是不应进入标题的正文",
            "committer": {"date": (NOW - timedelta(minutes=10)).isoformat()},
            "verification": {"verified": True},
        },
        "author": {"login": "kill-t"},
    }


def _pull(
    *,
    number: int = 12,
    updated_at: datetime | None = None,
    merged_at: datetime | None = None,
) -> dict[str, object]:
    selected_updated_at = updated_at or NOW - timedelta(minutes=4)
    selected_merged_at = merged_at or NOW - timedelta(minutes=5)
    return {
        "number": number,
        "title": "合并发布修复",
        "html_url": f"https://github.com/kill-t/ops-swarm/pull/{number}",
        "updated_at": selected_updated_at.isoformat(),
        "merged_at": selected_merged_at.isoformat(),
        "merge_commit_sha": MERGE_SHA,
        "user": {"login": "kill-t"},
    }


def _transport(
    *,
    commits: object | None = None,
    pulls: object | None = None,
    commit_status: int = 200,
    commit_link: str = "",
    pull_link: str = "",
) -> tuple[httpx2.MockTransport, list[httpx2.Request]]:
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        if request.url.path.endswith("/commits"):
            return httpx2.Response(
                commit_status,
                json=[_commit()] if commits is None else commits,
                headers={"link": commit_link},
            )
        if request.url.path.endswith("/pulls"):
            return httpx2.Response(
                200,
                json=[_pull()] if pulls is None else pulls,
                headers={"link": pull_link},
            )
        raise AssertionError(f"出现未授权 GitHub 路径：{request.url.path}")

    return httpx2.MockTransport(handler), requests


def _source(
    transport: httpx2.BaseTransport,
    *,
    token: str = "test-read-only-token",
) -> GitHubChangeOnlyInvestigationDataSource:
    return GitHubChangeOnlyInvestigationDataSource(
        GitHubChangeSettings(
            repository="kill-t/ops-swarm",
            token=SecretStr(token),
        ),
        transport=transport,
        clock=lambda: NOW_TIMESTAMP,
    )


def test_github_source_runs_two_fixed_gets_and_normalizes_changes() -> None:
    transport, requests = _transport()

    evidence = _source(transport).list_recent_changes(
        service_name="order-service",
        lookback_hours=6,
    )

    assert [item.evidence_id for item in evidence] == [
        f"github-commit-{COMMIT_SHA}",
        "github-pr-12",
    ]
    assert {item.source for item in evidence} == {"github-rest"}
    assert {item.attributes["change_type"] for item in evidence} == {
        "source_commit",
        "pull_request",
    }
    assert "\u0000" not in evidence[0].summary
    assert len(requests) == 2
    assert [request.method for request in requests] == ["GET", "GET"]
    assert [request.url.path for request in requests] == [
        "/repos/kill-t/ops-swarm/commits",
        "/repos/kill-t/ops-swarm/pulls",
    ]
    assert dict(requests[0].url.params) == {
        "since": "2026-08-31T06:00:00Z",
        "until": "2026-08-31T12:00:00Z",
        "per_page": "20",
        "page": "1",
    }
    assert dict(requests[1].url.params) == {
        "state": "closed",
        "sort": "updated",
        "direction": "desc",
        "per_page": "20",
        "page": "1",
    }
    assert requests[0].headers["x-github-api-version"] == GITHUB_API_VERSION
    assert requests[0].headers["authorization"] == "Bearer test-read-only-token"


def test_public_repository_mode_omits_authorization_header() -> None:
    transport, requests = _transport(commits=[], pulls=[])

    evidence = _source(transport, token="").list_recent_changes(
        service_name="order-service",
        lookback_hours=6,
    )

    assert evidence == ()
    assert "authorization" not in requests[0].headers


def test_unsupported_service_is_rejected_before_github_request() -> None:
    transport, requests = _transport()

    with pytest.raises(UnsupportedInvestigationTarget):
        _source(transport).list_recent_changes(
            service_name="payment-service",
            lookback_hours=6,
        )

    assert requests == []


@pytest.mark.parametrize(
    "repository",
    [
        "https://github.com/kill-t/ops-swarm",
        "kill-t/../ops-swarm",
        "kill-t/..",
        "kill-t/ops-swarm?tab=readme",
        "/kill-t/ops-swarm",
    ],
)
def test_settings_reject_dynamic_or_unsafe_repository(repository: str) -> None:
    with pytest.raises(ValidationError):
        GitHubChangeSettings(repository=repository)


@pytest.mark.parametrize(
    ("status", "headers", "error_code"),
    [
        (401, {}, "github_authentication_failed"),
        (403, {}, "github_access_denied"),
        (403, {"x-ratelimit-remaining": "0"}, "github_rate_limited"),
        (429, {"retry-after": "60"}, "github_rate_limited"),
        (404, {}, "github_repository_unavailable"),
        (503, {}, "github_unexpected_status"),
    ],
)
def test_http_failures_are_not_misreported_as_empty_changes(
    status: int,
    headers: dict[str, str],
    error_code: str,
) -> None:
    transport = httpx2.MockTransport(
        lambda request: httpx2.Response(status, headers=headers, request=request)
    )

    with pytest.raises(InvestigationDataSourceFailure) as captured:
        _source(transport).list_recent_changes(
            service_name="order-service",
            lookback_hours=6,
        )

    assert captured.value.error_code == error_code
    assert captured.value.http_status == status


def test_invalid_json_and_large_response_fail_closed() -> None:
    invalid = httpx2.MockTransport(
        lambda request: httpx2.Response(200, content=b"not-json", request=request)
    )
    large = httpx2.MockTransport(
        lambda request: httpx2.Response(
            200,
            content=b"x" * (MAX_RESPONSE_BYTES + 1),
            request=request,
        )
    )

    with pytest.raises(InvestigationDataSourceFailure) as invalid_error:
        _source(invalid).list_recent_changes(service_name="order-service", lookback_hours=6)
    with pytest.raises(InvestigationDataSourceFailure) as large_error:
        _source(large).list_recent_changes(service_name="order-service", lookback_hours=6)

    assert invalid_error.value.error_code == "github_invalid_response"
    assert large_error.value.error_code == "github_response_too_large"


def test_commit_pagination_and_repository_prefix_collision_fail_closed() -> None:
    next_page = '<https://api.github.com/repositories/1/commits?page=2>; rel="next"'
    paged_transport, _ = _transport(commit_link=next_page)
    unsafe_transport, _ = _transport(
        commits=[_commit(html_url=f"https://github.com/kill-t/ops-swarmevil/commit/{COMMIT_SHA}")]
    )

    with pytest.raises(InvestigationDataSourceFailure) as paged:
        _source(paged_transport).list_recent_changes(service_name="order-service", lookback_hours=6)
    with pytest.raises(InvestigationDataSourceFailure) as unsafe:
        _source(unsafe_transport).list_recent_changes(
            service_name="order-service", lookback_hours=6
        )

    assert paged.value.error_code == "github_result_truncated"
    assert unsafe.value.error_code == "github_invalid_url"


def test_pull_pagination_is_accepted_only_after_crossing_time_window() -> None:
    next_page = '<https://api.github.com/repositories/1/pulls?page=2>; rel="next"'
    old_time = NOW - timedelta(hours=7)
    complete_transport, _ = _transport(
        commits=[],
        pulls=[_pull(updated_at=old_time, merged_at=old_time - timedelta(minutes=1))],
        pull_link=next_page,
    )
    incomplete_transport, _ = _transport(pull_link=next_page)

    assert (
        _source(complete_transport).list_recent_changes(
            service_name="order-service", lookback_hours=6
        )
        == ()
    )
    with pytest.raises(InvestigationDataSourceFailure) as incomplete:
        _source(incomplete_transport).list_recent_changes(
            service_name="order-service", lookback_hours=6
        )

    assert incomplete.value.error_code == "github_result_truncated"


class FixedChanges:
    """只为组合器测试返回固定变更证据。"""

    def __init__(self, items: Sequence[EvidenceObservation]) -> None:
        self.items = items

    def query_logs(
        self, *, service_name: str, lookback_minutes: int
    ) -> Sequence[EvidenceObservation]:
        del service_name, lookback_minutes
        return ()

    def query_metrics(
        self, *, service_name: str, lookback_minutes: int
    ) -> Sequence[EvidenceObservation]:
        del service_name, lookback_minutes
        return ()

    def list_recent_changes(
        self, *, service_name: str, lookback_hours: int
    ) -> Sequence[EvidenceObservation]:
        del service_name, lookback_hours
        return self.items


def _fixed_evidence(identifier: str, summary: str) -> EvidenceObservation:
    return EvidenceObservation(
        evidence_id=identifier,
        kind=EvidenceKind.CHANGE,
        source="fixed-test",
        summary=summary,
        observed_at=NOW,
    )


def test_enriched_source_preserves_both_sources_and_rejects_identifier_conflict() -> None:
    simulator = _fixed_evidence("simulator-change", "模拟器变更")
    github = _fixed_evidence("github-change", "GitHub 变更")
    source = GitHubEnrichedChangeDataSource(FixedChanges([simulator]), FixedChanges([github]))

    result = source.list_recent_changes(service_name="order-service", lookback_hours=6)

    assert {item.evidence_id for item in result} == {"simulator-change", "github-change"}

    conflicting = _fixed_evidence("simulator-change", "冲突变更")
    conflict_source = GitHubEnrichedChangeDataSource(
        FixedChanges([simulator]), FixedChanges([conflicting])
    )
    with pytest.raises(InvestigationDataSourceFailure) as captured:
        conflict_source.list_recent_changes(service_name="order-service", lookback_hours=6)

    assert captured.value.error_code == "change_evidence_conflict"
