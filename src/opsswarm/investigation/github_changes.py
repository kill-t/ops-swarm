"""把固定 GitHub 仓库的只读提交与合并请求转换为变更证据。"""

from __future__ import annotations

import re
import time
import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from typing import Annotated, Any, Self, cast
from urllib.parse import urlsplit

import httpx2
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    SecretStr,
    TypeAdapter,
    ValidationError,
    field_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

from opsswarm.change_records.models import SUPPORTED_SERVICE
from opsswarm.investigation.errors import (
    InvestigationDataSourceFailure,
    UnsupportedInvestigationTarget,
)
from opsswarm.investigation.models import EvidenceKind, EvidenceObservation
from opsswarm.investigation.tools import InvestigationDataSource

GITHUB_API_BASE_URL = "https://api.github.com"
GITHUB_API_VERSION = "2026-03-10"
MAX_ITEMS_PER_KIND = 20
MAX_RESPONSE_BYTES = 256 * 1024
MAX_FUTURE_CLOCK_SKEW_SECONDS = 5.0
_REPOSITORY_PATTERN = re.compile(
    r"^(?P<owner>[A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))/(?P<repo>[A-Za-z0-9_.-]{1,100})$"
)
_SHA_PATTERN = r"^[0-9a-fA-F]{40,64}$"
GitHubSha = Annotated[str, Field(pattern=_SHA_PATTERN)]


class GitHubChangeSettings(BaseSettings):
    """固定 GitHub 仓库与可选只读令牌。"""

    model_config = SettingsConfigDict(env_prefix="OPSSWARM_GITHUB_", extra="ignore")

    repository: str = Field(min_length=3, max_length=140)
    token: SecretStr = Field(default_factory=lambda: SecretStr(""))
    request_timeout_seconds: float = Field(default=5.0, gt=0, le=15)

    @field_validator("repository")
    @classmethod
    def validate_repository(cls, value: str) -> str:
        """只接受一个 owner/repo，不允许 URL、路径穿越或动态查询。"""

        normalized = value.strip()
        if _REPOSITORY_PATTERN.fullmatch(normalized) is None:
            raise ValueError("GitHub 仓库必须使用安全的 owner/repo 格式")
        if normalized.split("/", maxsplit=1)[1] in {".", ".."}:
            raise ValueError("GitHub 仓库名不能是相对路径标记")
        return normalized

    @property
    def owner(self) -> str:
        return self.repository.split("/", maxsplit=1)[0]

    @property
    def repo(self) -> str:
        return self.repository.split("/", maxsplit=1)[1]

    @classmethod
    def from_dotenv(cls) -> Self:
        """仅供显式本地命令读取项目根目录的 .env。"""

        constructor = cast(Any, cls)
        return cast(Self, constructor(_env_file=".env", _env_file_encoding="utf-8"))


class _ExternalModel(BaseModel):
    """只校验实际消费的 GitHub 字段，忽略官方新增字段。"""

    model_config = ConfigDict(extra="ignore")


class _GitHubActor(_ExternalModel):
    login: str = Field(
        min_length=1,
        max_length=39,
        pattern=r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$",
    )


class _GitHubCommitIdentity(_ExternalModel):
    date: datetime

    @field_validator("date")
    @classmethod
    def normalize_date(cls, value: datetime) -> datetime:
        return _normalize_external_time(value)


class _GitHubVerification(_ExternalModel):
    verified: bool


class _GitHubCommitDetails(_ExternalModel):
    message: str = Field(max_length=128 * 1024)
    committer: _GitHubCommitIdentity
    verification: _GitHubVerification


class _GitHubCommit(_ExternalModel):
    sha: GitHubSha
    html_url: str = Field(min_length=1, max_length=500)
    commit: _GitHubCommitDetails
    author: _GitHubActor | None = None


class _GitHubPullRequest(_ExternalModel):
    number: int = Field(ge=1, le=9_223_372_036_854_775_807)
    title: str = Field(max_length=128 * 1024)
    html_url: str = Field(min_length=1, max_length=500)
    updated_at: datetime
    merged_at: datetime | None
    merge_commit_sha: GitHubSha | None = None
    user: _GitHubActor | None = None

    @field_validator("updated_at", "merged_at")
    @classmethod
    def normalize_time(cls, value: datetime | None) -> datetime | None:
        return _normalize_external_time(value) if value is not None else None


_COMMITS_ADAPTER = TypeAdapter(list[_GitHubCommit])
_PULLS_ADAPTER = TypeAdapter(list[_GitHubPullRequest])


@dataclass(frozen=True, slots=True)
class _GitHubPage:
    payload: bytes
    link_header: str


class GitHubChangeOnlyInvestigationDataSource:
    """只对一个配置仓库执行两个固定 GET 请求。"""

    def __init__(
        self,
        settings: GitHubChangeSettings,
        *,
        transport: httpx2.BaseTransport | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._settings = settings
        self._transport = transport
        self._clock = clock

    def query_logs(
        self,
        *,
        service_name: str,
        lookback_minutes: int,
    ) -> Sequence[EvidenceObservation]:
        self._validate_target(service_name)
        self._validate_minute_lookback(lookback_minutes)
        return ()

    def query_metrics(
        self,
        *,
        service_name: str,
        lookback_minutes: int,
    ) -> Sequence[EvidenceObservation]:
        self._validate_target(service_name)
        self._validate_minute_lookback(lookback_minutes)
        return ()

    def list_recent_changes(
        self,
        *,
        service_name: str,
        lookback_hours: int,
    ) -> Sequence[EvidenceObservation]:
        """串行读取提交和关闭的 PR，并拒绝不完整时间窗。"""

        self._validate_target(service_name)
        self._validate_hour_lookback(lookback_hours)
        request_time = datetime.fromtimestamp(self._clock(), UTC)
        earliest = request_time - timedelta(hours=lookback_hours)
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
            "User-Agent": "OpsSwarm/0.1",
        }
        token = self._settings.token.get_secret_value()
        if token:
            headers["Authorization"] = f"Bearer {token}"

        try:
            with httpx2.Client(
                base_url=GITHUB_API_BASE_URL,
                headers=headers,
                timeout=self._settings.request_timeout_seconds,
                transport=self._transport,
                follow_redirects=True,
                trust_env=False,
            ) as client:
                commits_page = self._get(
                    client,
                    f"/repos/{self._settings.owner}/{self._settings.repo}/commits",
                    params={
                        "since": earliest.isoformat().replace("+00:00", "Z"),
                        "until": request_time.isoformat().replace("+00:00", "Z"),
                        "per_page": MAX_ITEMS_PER_KIND,
                        "page": 1,
                    },
                )
                pulls_page = self._get(
                    client,
                    f"/repos/{self._settings.owner}/{self._settings.repo}/pulls",
                    params={
                        "state": "closed",
                        "sort": "updated",
                        "direction": "desc",
                        "per_page": MAX_ITEMS_PER_KIND,
                        "page": 1,
                    },
                )
        except httpx2.TimeoutException as error:
            raise InvestigationDataSourceFailure(
                error_code="github_query_timeout",
                message="GitHub 变更查询超时",
            ) from error
        except httpx2.HTTPError as error:
            raise InvestigationDataSourceFailure(
                error_code="github_query_unavailable",
                message="GitHub 变更查询不可用",
            ) from error

        commits = self._parse_commits(commits_page, earliest=earliest, request_time=request_time)
        pulls = self._parse_pulls(pulls_page, earliest=earliest, request_time=request_time)
        observations = [self._commit_evidence(item) for item in commits]
        observations.extend(self._pull_evidence(item) for item in pulls)
        evidence_ids: set[str] = set()
        for item in observations:
            if item.evidence_id in evidence_ids:
                raise InvestigationDataSourceFailure(
                    error_code="github_duplicate_change",
                    message="GitHub 变更响应包含重复证据",
                )
            evidence_ids.add(item.evidence_id)
        return tuple(sorted(observations, key=lambda item: (item.observed_at, item.evidence_id)))

    def _get(
        self,
        client: httpx2.Client,
        path: str,
        *,
        params: dict[str, str | int],
    ) -> _GitHubPage:
        with client.stream("GET", path, params=params) as response:
            final_url = urlsplit(str(response.url))
            if final_url.scheme != "https" or final_url.hostname != "api.github.com":
                raise InvestigationDataSourceFailure(
                    error_code="github_unsafe_redirect",
                    message="GitHub 查询被重定向到非白名单地址",
                )
            if response.status_code == 401:
                raise InvestigationDataSourceFailure(
                    error_code="github_authentication_failed",
                    message="GitHub 只读凭据无效",
                    http_status=401,
                )
            if response.status_code in {403, 429}:
                rate_limited = (
                    response.status_code == 429
                    or response.headers.get("retry-after") is not None
                    or response.headers.get("x-ratelimit-remaining") == "0"
                )
                raise InvestigationDataSourceFailure(
                    error_code="github_rate_limited" if rate_limited else "github_access_denied",
                    message=("GitHub 查询受到限流" if rate_limited else "GitHub 只读访问被拒绝"),
                    http_status=response.status_code,
                )
            if response.status_code == 404:
                raise InvestigationDataSourceFailure(
                    error_code="github_repository_unavailable",
                    message="GitHub 仓库不存在或当前凭据无权读取",
                    http_status=404,
                )
            if response.status_code != 200:
                raise InvestigationDataSourceFailure(
                    error_code="github_unexpected_status",
                    message="GitHub 变更查询返回了非预期状态",
                    http_status=response.status_code,
                )
            payload = bytearray()
            for chunk in response.iter_bytes():
                if len(payload) + len(chunk) > MAX_RESPONSE_BYTES:
                    raise InvestigationDataSourceFailure(
                        error_code="github_response_too_large",
                        message="GitHub 变更响应超过允许大小",
                    )
                payload.extend(chunk)
            return _GitHubPage(
                payload=bytes(payload),
                link_header=response.headers.get("link", ""),
            )

    def _parse_commits(
        self,
        page: _GitHubPage,
        *,
        earliest: datetime,
        request_time: datetime,
    ) -> tuple[_GitHubCommit, ...]:
        try:
            commits = _COMMITS_ADAPTER.validate_json(page.payload)
        except (ValidationError, ValueError) as error:
            raise InvestigationDataSourceFailure(
                error_code="github_invalid_response",
                message="GitHub 提交响应不符合契约",
            ) from error
        if len(commits) > MAX_ITEMS_PER_KIND or _has_next_page(page.link_header):
            raise InvestigationDataSourceFailure(
                error_code="github_result_truncated",
                message="GitHub 提交超过单次调查上限，不能形成完整时间线",
            )
        seen: set[str] = set()
        for item in commits:
            if item.sha in seen:
                raise InvestigationDataSourceFailure(
                    error_code="github_duplicate_change",
                    message="GitHub 提交响应包含重复 SHA",
                )
            seen.add(item.sha)
            occurred_at = item.commit.committer.date
            if (
                not earliest
                <= occurred_at
                <= request_time + timedelta(seconds=MAX_FUTURE_CLOCK_SKEW_SECONDS)
            ):
                raise InvestigationDataSourceFailure(
                    error_code="github_invalid_timestamp",
                    message="GitHub 提交时间不在请求窗口内",
                )
            self._validate_html_url(item.html_url, suffix=f"/commit/{item.sha}")
        return tuple(commits)

    def _parse_pulls(
        self,
        page: _GitHubPage,
        *,
        earliest: datetime,
        request_time: datetime,
    ) -> tuple[_GitHubPullRequest, ...]:
        try:
            pulls = _PULLS_ADAPTER.validate_json(page.payload)
        except (ValidationError, ValueError) as error:
            raise InvestigationDataSourceFailure(
                error_code="github_invalid_response",
                message="GitHub Pull Request 响应不符合契约",
            ) from error
        if len(pulls) > MAX_ITEMS_PER_KIND:
            raise InvestigationDataSourceFailure(
                error_code="github_result_truncated",
                message="GitHub Pull Request 超过单次调查上限",
            )
        if any(left.updated_at < right.updated_at for left, right in pairwise(pulls)):
            raise InvestigationDataSourceFailure(
                error_code="github_invalid_order",
                message="GitHub Pull Request 未按更新时间倒序返回",
            )
        if _has_next_page(page.link_header) and (not pulls or pulls[-1].updated_at >= earliest):
            raise InvestigationDataSourceFailure(
                error_code="github_result_truncated",
                message="GitHub Pull Request 时间窗结果不完整",
            )

        seen: set[int] = set()
        relevant: list[_GitHubPullRequest] = []
        for item in pulls:
            if item.number in seen:
                raise InvestigationDataSourceFailure(
                    error_code="github_duplicate_change",
                    message="GitHub Pull Request 响应包含重复编号",
                )
            seen.add(item.number)
            if item.updated_at > request_time + timedelta(seconds=MAX_FUTURE_CLOCK_SKEW_SECONDS):
                raise InvestigationDataSourceFailure(
                    error_code="github_invalid_timestamp",
                    message="GitHub Pull Request 更新时间晚于调查时间",
                )
            self._validate_html_url(item.html_url, suffix=f"/pull/{item.number}")
            if item.merged_at is None or item.merged_at < earliest:
                continue
            if not item.merged_at <= item.updated_at:
                raise InvestigationDataSourceFailure(
                    error_code="github_invalid_timestamp",
                    message="GitHub Pull Request 合并时间晚于更新时间",
                )
            relevant.append(item)
        return tuple(relevant)

    def _commit_evidence(self, item: _GitHubCommit) -> EvidenceObservation:
        title = _safe_title(item.commit.message)
        external_id = f"github-commit-{item.sha.lower()}"
        attributes: dict[str, JsonValue] = {
            "change_type": "source_commit",
            "external_change_id": external_id,
            "repository": self._settings.repository,
            "commit_sha": item.sha.lower(),
            "html_url": item.html_url,
            "title": title,
            "signature_verified": item.commit.verification.verified,
            "completed": True,
        }
        if item.author is not None:
            attributes["author_login"] = item.author.login
        return EvidenceObservation(
            evidence_id=external_id,
            kind=EvidenceKind.CHANGE,
            source="github-rest",
            summary=f"GitHub 提交 {item.sha[:7]}：{title}",
            observed_at=item.commit.committer.date,
            attributes=attributes,
        )

    def _pull_evidence(self, item: _GitHubPullRequest) -> EvidenceObservation:
        assert item.merged_at is not None
        title = _safe_title(item.title)
        external_id = f"github-pr-{item.number}"
        attributes: dict[str, JsonValue] = {
            "change_type": "pull_request",
            "external_change_id": external_id,
            "repository": self._settings.repository,
            "pull_number": item.number,
            "html_url": item.html_url,
            "title": title,
            "completed": True,
        }
        if item.merge_commit_sha is not None:
            attributes["merge_commit_sha"] = item.merge_commit_sha.lower()
        if item.user is not None:
            attributes["author_login"] = item.user.login
        return EvidenceObservation(
            evidence_id=external_id,
            kind=EvidenceKind.CHANGE,
            source="github-rest",
            summary=f"GitHub Pull Request #{item.number} 已合并：{title}",
            observed_at=item.merged_at,
            attributes=attributes,
        )

    def _validate_html_url(self, value: str, *, suffix: str) -> None:
        parsed = urlsplit(value)
        expected_prefix = f"/{self._settings.owner}/{self._settings.repo}"
        if (
            parsed.scheme != "https"
            or parsed.hostname != "github.com"
            or parsed.port is not None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path.lower() != f"{expected_prefix}{suffix}".lower()
        ):
            raise InvestigationDataSourceFailure(
                error_code="github_invalid_url",
                message="GitHub 变更响应包含非白名单页面地址",
            )

    @staticmethod
    def _validate_target(service_name: str) -> None:
        if service_name != SUPPORTED_SERVICE:
            raise UnsupportedInvestigationTarget(service_name)

    @staticmethod
    def _validate_minute_lookback(lookback_minutes: int) -> None:
        if isinstance(lookback_minutes, bool) or not isinstance(lookback_minutes, int):
            raise ValueError("lookback_minutes 必须是整数")
        if not 1 <= lookback_minutes <= 60:
            raise ValueError("lookback_minutes 必须在 1 到 60 之间")

    @staticmethod
    def _validate_hour_lookback(lookback_hours: int) -> None:
        if isinstance(lookback_hours, bool) or not isinstance(lookback_hours, int):
            raise ValueError("lookback_hours 必须是整数")
        if not 1 <= lookback_hours <= 24:
            raise ValueError("lookback_hours 必须在 1 到 24 之间")


class GitHubEnrichedChangeDataSource:
    """保留确定性变更事实，并把 GitHub 作为必需的只读补充来源。"""

    def __init__(
        self,
        simulator: InvestigationDataSource,
        github: InvestigationDataSource,
    ) -> None:
        self._simulator = simulator
        self._github = github

    def query_logs(
        self,
        *,
        service_name: str,
        lookback_minutes: int,
    ) -> Sequence[EvidenceObservation]:
        return self._simulator.query_logs(
            service_name=service_name,
            lookback_minutes=lookback_minutes,
        )

    def query_metrics(
        self,
        *,
        service_name: str,
        lookback_minutes: int,
    ) -> Sequence[EvidenceObservation]:
        return self._simulator.query_metrics(
            service_name=service_name,
            lookback_minutes=lookback_minutes,
        )

    def list_recent_changes(
        self,
        *,
        service_name: str,
        lookback_hours: int,
    ) -> Sequence[EvidenceObservation]:
        simulator_items = self._simulator.list_recent_changes(
            service_name=service_name,
            lookback_hours=lookback_hours,
        )
        github_items = self._github.list_recent_changes(
            service_name=service_name,
            lookback_hours=lookback_hours,
        )
        by_id: dict[str, EvidenceObservation] = {}
        for item in (*simulator_items, *github_items):
            existing = by_id.get(item.evidence_id)
            if existing is not None and existing != item:
                raise InvestigationDataSourceFailure(
                    error_code="change_evidence_conflict",
                    message="组合变更来源返回了冲突证据标识",
                )
            by_id[item.evidence_id] = item
        return tuple(sorted(by_id.values(), key=lambda item: (item.observed_at, item.evidence_id)))


def _normalize_external_time(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("GitHub 时间必须包含时区")
    return value.astimezone(UTC)


def _has_next_page(link_header: str) -> bool:
    return any('rel="next"' in part for part in link_header.split(","))


def _safe_title(value: str) -> str:
    first_nonempty = next((line.strip() for line in value.splitlines() if line.strip()), "无标题")
    without_controls = "".join(
        " " if unicodedata.category(character).startswith("C") else character
        for character in first_nonempty
    )
    normalized = " ".join(without_controls.split()) or "无标题"
    if len(normalized) <= 240:
        return normalized
    return f"{normalized[:239]}…"
