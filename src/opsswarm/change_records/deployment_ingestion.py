"""受限 CI/CD 部署事件的签名、白名单与时间契约。"""

from __future__ import annotations

import hashlib
import hmac
import re
from datetime import UTC, datetime
from typing import Any, Literal, Self, cast

from fastapi import Request, status
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

from opsswarm.change_records.models import (
    LOCAL_ADMIN_SOURCE_IDENTITY,
    CommitSha,
    DeploymentChange,
    DeploymentCodeReference,
    ExternalChangeId,
    ReleaseVersion,
    RepositorySlug,
)

MAX_DEPLOYMENT_EVENT_BYTES = 16 * 1024
_KEY_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_TIMESTAMP_PATTERN = re.compile(r"^[1-9][0-9]{9,11}$")
_SIGNATURE_PATTERN = re.compile(r"^sha256=(?P<digest>[0-9a-f]{64})$")


class DeploymentIngestionSettings(BaseSettings):
    """默认关闭的单一 CI/CD 身份与固定仓库白名单。"""

    model_config = SettingsConfigDict(
        env_prefix="OPSSWARM_DEPLOYMENT_INGESTION_",
        extra="ignore",
    )

    enabled: bool = False
    key_id: str = ""
    secret: SecretStr = Field(default_factory=lambda: SecretStr(""))
    repository: str = ""
    max_signature_age_seconds: int = Field(default=300, ge=30, le=900)
    max_event_age_seconds: int = Field(default=86_400, ge=60, le=604_800)

    @model_validator(mode="after")
    def require_complete_enabled_configuration(self) -> Self:
        if not self.enabled:
            return self
        if (
            _KEY_ID_PATTERN.fullmatch(self.key_id) is None
            or self.key_id == LOCAL_ADMIN_SOURCE_IDENTITY
        ):
            raise ValueError("CI/CD key_id 必须是非保留的安全来源身份")
        secret_bytes = self.secret.get_secret_value().encode("utf-8")
        if not 32 <= len(secret_bytes) <= 256:
            raise ValueError("CI/CD HMAC 密钥必须为 32 到 256 字节")
        try:
            DeploymentCodeReference(repository=self.repository, commit_sha="0" * 40)
        except ValidationError as error:
            raise ValueError("CI/CD 固定仓库必须使用安全的 owner/repo 格式") from error
        return self

    @classmethod
    def from_dotenv(cls) -> Self:
        """仅供显式本地命令读取项目根目录的 .env。"""

        constructor = cast(Any, cls)
        return cast(Self, constructor(_env_file=".env", _env_file_encoding="utf-8"))


class DeploymentEvent(BaseModel):
    """成功部署完成后由 CI/CD 身份提交的最小不可变事实。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    delivery_id: ExternalChangeId
    environment: Literal["fault-lab"]
    service_name: Literal["order-service"]
    repository: RepositorySlug
    commit_sha: CommitSha
    from_version: ReleaseVersion
    to_version: ReleaseVersion
    deployment_status: Literal["succeeded"]
    deployed_at: datetime

    @field_validator("repository")
    @classmethod
    def reject_relative_repository(cls, value: str) -> str:
        if value.split("/", maxsplit=1)[1] in {".", ".."}:
            raise ValueError("代码仓库名不能是相对路径标记")
        return value

    @field_validator("deployed_at")
    @classmethod
    def normalize_deployed_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("deployed_at 必须包含时区")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def require_version_change(self) -> Self:
        if self.from_version == self.to_version:
            raise ValueError("发布前后版本不能相同")
        return self

    def to_change(self, *, repository: str) -> DeploymentChange:
        """使用服务端白名单仓库构造既有不可变部署事实。"""

        return DeploymentChange(
            external_change_id=self.delivery_id,
            from_version=self.from_version,
            to_version=self.to_version,
            code_reference=DeploymentCodeReference(
                repository=repository,
                commit_sha=self.commit_sha,
            ),
        )


class DeploymentIngestionError(Exception):
    """签名或部署事件契约被拒绝时返回的稳定有限错误。"""

    def __init__(self, *, status_code: int, error_code: str, message: str) -> None:
        self.status_code = status_code
        self.error_code = error_code
        self.message = message
        super().__init__(message)


async def authenticate_deployment_event(
    request: Request,
    *,
    settings: DeploymentIngestionSettings,
    current_time: datetime,
    key_id: str | None,
    timestamp: str | None,
    signature: str | None,
) -> DeploymentEvent:
    """先验证原始请求身份，再解析并校验有限部署事实。"""

    if not settings.enabled:
        raise DeploymentIngestionError(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            error_code="deployment_ingestion_disabled",
            message="CI/CD 部署事件摄取未启用",
        )
    now = _normalize_current_time(current_time)
    _validate_content_type(request)
    signed_at = _authenticate_headers(
        settings=settings,
        current_time=now,
        key_id=key_id,
        timestamp=timestamp,
        signature=signature,
    )
    payload = await _read_limited_body(request)
    _verify_signature(
        settings=settings,
        timestamp=signed_at,
        signature=signature,
        payload=payload,
    )
    try:
        event = DeploymentEvent.model_validate_json(payload)
    except (ValidationError, ValueError) as error:
        raise DeploymentIngestionError(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            error_code="deployment_event_invalid",
            message="部署事件不符合固定契约",
        ) from error
    if event.repository != settings.repository:
        raise DeploymentIngestionError(
            status_code=status.HTTP_403_FORBIDDEN,
            error_code="deployment_repository_forbidden",
            message="部署事件仓库不在固定白名单中",
        )
    age_seconds = (now - event.deployed_at).total_seconds()
    if age_seconds < 0 or age_seconds > settings.max_event_age_seconds:
        raise DeploymentIngestionError(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            error_code="deployment_event_timestamp_invalid",
            message="部署发生时间晚于接收时间或已超过允许时效",
        )
    return event


def _normalize_current_time(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise RuntimeError("部署事件服务端时钟必须包含时区")
    return value.astimezone(UTC)


def _validate_content_type(request: Request) -> None:
    content_type = request.headers.get("content-type", "").split(";", maxsplit=1)[0].strip()
    if content_type.lower() != "application/json":
        raise DeploymentIngestionError(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            error_code="deployment_event_media_type_invalid",
            message="部署事件只接受 application/json",
        )


def _authenticate_headers(
    *,
    settings: DeploymentIngestionSettings,
    current_time: datetime,
    key_id: str | None,
    timestamp: str | None,
    signature: str | None,
) -> str:
    configured_key_id = settings.key_id
    if (
        key_id is None
        or timestamp is None
        or signature is None
        or not hmac.compare_digest(key_id, configured_key_id)
        or _TIMESTAMP_PATTERN.fullmatch(timestamp) is None
        or _SIGNATURE_PATTERN.fullmatch(signature) is None
    ):
        raise _authentication_error()
    try:
        signed_at = datetime.fromtimestamp(int(timestamp), UTC)
    except (OverflowError, OSError, ValueError) as error:
        raise _authentication_error() from error
    if abs((current_time - signed_at).total_seconds()) > settings.max_signature_age_seconds:
        raise DeploymentIngestionError(
            status_code=status.HTTP_401_UNAUTHORIZED,
            error_code="deployment_signature_expired",
            message="部署事件签名时间戳已过期或超前",
        )
    return timestamp


async def _read_limited_body(request: Request) -> bytes:
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared_length = int(content_length)
        except ValueError as error:
            raise DeploymentIngestionError(
                status_code=status.HTTP_400_BAD_REQUEST,
                error_code="deployment_request_invalid",
                message="部署事件 Content-Length 不合法",
            ) from error
        if declared_length < 0:
            raise DeploymentIngestionError(
                status_code=status.HTTP_400_BAD_REQUEST,
                error_code="deployment_request_invalid",
                message="部署事件 Content-Length 不合法",
            )
        if declared_length > MAX_DEPLOYMENT_EVENT_BYTES:
            raise _payload_too_large_error()

    payload = bytearray()
    async for chunk in request.stream():
        payload.extend(chunk)
        if len(payload) > MAX_DEPLOYMENT_EVENT_BYTES:
            raise _payload_too_large_error()
    return bytes(payload)


def _verify_signature(
    *,
    settings: DeploymentIngestionSettings,
    timestamp: str,
    signature: str | None,
    payload: bytes,
) -> None:
    assert signature is not None
    expected = hmac.new(
        settings.secret.get_secret_value().encode("utf-8"),
        timestamp.encode("ascii") + b"." + payload,
        hashlib.sha256,
    ).hexdigest()
    supplied = _SIGNATURE_PATTERN.fullmatch(signature)
    assert supplied is not None
    if not hmac.compare_digest(supplied.group("digest"), expected):
        raise _authentication_error()


def _authentication_error() -> DeploymentIngestionError:
    return DeploymentIngestionError(
        status_code=status.HTTP_401_UNAUTHORIZED,
        error_code="deployment_authentication_failed",
        message="部署事件认证失败",
    )


def _payload_too_large_error() -> DeploymentIngestionError:
    return DeploymentIngestionError(
        status_code=status.HTTP_413_CONTENT_TOO_LARGE,
        error_code="deployment_event_too_large",
        message="部署事件超过 16 KiB 上限",
    )
