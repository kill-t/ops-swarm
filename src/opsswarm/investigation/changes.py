"""把本地变更模拟服务的白名单记录转换为调查证据。"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

import httpx2
from pydantic import Field, JsonValue, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from opsswarm.change_records.models import (
    SUPPORTED_SERVICE,
    ChangeRecord,
    ChangeRecordListResponse,
    ConfigurationChange,
    DependencyMaintenanceChange,
    DeploymentArtifactReference,
    DeploymentChange,
)
from opsswarm.fault_lab.runtime_metadata import (
    RuntimeArtifactInstance,
    RuntimeInventoryResponse,
    RuntimeInventoryUnavailableResponse,
)
from opsswarm.investigation.errors import (
    InvestigationDataSourceFailure,
    UnsupportedInvestigationTarget,
)
from opsswarm.investigation.models import EvidenceKind, EvidenceObservation

DEFAULT_CHANGE_BASE_URL = "http://127.0.0.1:18002"
DEFAULT_RUNTIME_BASE_URL = "http://127.0.0.1:18001"
ALLOWED_CHANGE_HOSTS = frozenset({"127.0.0.1", "localhost", "change-service"})
ALLOWED_RUNTIME_HOSTS = frozenset({"127.0.0.1", "localhost", "order-service"})
MAX_RESPONSE_BYTES = 128 * 1024
MAX_RUNTIME_INVENTORY_RESPONSE_BYTES = 64 * 1024
MAX_RESPONSE_AGE_SECONDS = 10.0
MAX_FUTURE_CLOCK_SKEW_SECONDS = 5.0

RuntimeArtifactStatus = Literal[
    "matched",
    "rolling_update",
    "partial_mismatch",
    "mismatched",
]


@dataclass(frozen=True)
class RuntimeArtifactAssessment:
    """多实例身份与当前、上一部署产物的确定性分类结果。"""

    status: RuntimeArtifactStatus
    matching_count: int
    previous_count: int
    unexpected_count: int
    instances: list[JsonValue]


class ChangeInvestigationSettings(BaseSettings):
    """限制调查适配器只能访问本地故障实验室变更服务。"""

    model_config = SettingsConfigDict(env_prefix="OPSSWARM_INVESTIGATION_", extra="ignore")

    change_base_url: str = Field(default=DEFAULT_CHANGE_BASE_URL, min_length=1)
    runtime_base_url: str = Field(default=DEFAULT_RUNTIME_BASE_URL, min_length=1)
    request_timeout_seconds: float = Field(default=3.0, gt=0, le=10)

    @field_validator("change_base_url")
    @classmethod
    def restrict_change_url(cls, value: str) -> str:
        normalized = value.rstrip("/")
        parsed = urlsplit(normalized)
        if parsed.scheme != "http":
            raise ValueError("变更调查地址只允许 http")
        if parsed.hostname not in ALLOWED_CHANGE_HOSTS:
            raise ValueError("变更调查地址只允许 localhost、127.0.0.1 或 change-service")
        if (
            parsed.hostname in {"localhost", "127.0.0.1"}
            and parsed.port != 18002
            or parsed.hostname == "change-service"
            and parsed.port != 8000
        ):
            raise ValueError("变更调查地址端口必须匹配本机 18002 或 Compose 8000")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("变更调查地址不能包含用户信息")
        if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            raise ValueError("变更调查地址不能包含路径、查询参数或片段")
        return normalized

    @field_validator("runtime_base_url")
    @classmethod
    def restrict_runtime_url(cls, value: str) -> str:
        normalized = value.rstrip("/")
        parsed = urlsplit(normalized)
        if parsed.scheme != "http":
            raise ValueError("运行元数据地址只允许 http")
        if parsed.hostname not in ALLOWED_RUNTIME_HOSTS:
            raise ValueError("运行元数据地址只允许 localhost、127.0.0.1 或 order-service")
        if (
            parsed.hostname in {"localhost", "127.0.0.1"}
            and parsed.port != 18001
            or parsed.hostname == "order-service"
            and parsed.port != 8000
        ):
            raise ValueError("运行元数据地址端口必须匹配本机 18001 或 Compose 8000")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("运行元数据地址不能包含用户信息")
        if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            raise ValueError("运行元数据地址不能包含路径、查询参数或片段")
        return normalized


class ChangeApiOnlyInvestigationDataSource:
    """只读取变更事实；尚未组合的日志和指标端口明确返回空集合。"""

    def __init__(
        self,
        settings: ChangeInvestigationSettings | None = None,
        *,
        transport: httpx2.BaseTransport | None = None,
        runtime_transport: httpx2.BaseTransport | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._settings = settings if settings is not None else ChangeInvestigationSettings()
        self._transport = transport
        self._runtime_transport = runtime_transport
        self._clock = clock

    def query_logs(
        self,
        *,
        service_name: str,
        lookback_minutes: int,
    ) -> Sequence[EvidenceObservation]:
        """组合数据源尚未接入时不伪造日志证据。"""

        self._validate_target(service_name)
        self._validate_minute_lookback(lookback_minutes)
        return ()

    def query_metrics(
        self,
        *,
        service_name: str,
        lookback_minutes: int,
    ) -> Sequence[EvidenceObservation]:
        """组合数据源尚未接入时不伪造指标证据。"""

        self._validate_target(service_name)
        self._validate_minute_lookback(lookback_minutes)
        return ()

    def list_recent_changes(
        self,
        *,
        service_name: str,
        lookback_hours: int,
    ) -> Sequence[EvidenceObservation]:
        """执行固定只读查询，并对响应时间线和内容做失败关闭校验。"""

        self._validate_target(service_name)
        self._validate_hour_lookback(lookback_hours)
        request_time = datetime.fromtimestamp(self._clock(), UTC)
        payload = self._get_changes(
            params={
                "service_name": SUPPORTED_SERVICE,
                "lookback_hours": lookback_hours,
            }
        )
        try:
            response = ChangeRecordListResponse.model_validate_json(payload)
        except (ValidationError, ValueError) as error:
            raise InvestigationDataSourceFailure(
                error_code="change_invalid_response",
                message="变更调查查询返回了不符合契约的响应",
            ) from error
        self._validate_response_time(response, request_time=request_time)
        if response.truncated:
            raise InvestigationDataSourceFailure(
                error_code="change_result_truncated",
                message="变更调查结果已截断，不能据此形成完整时间线",
            )

        earliest = response.generated_at - timedelta(hours=lookback_hours)
        record_ids: set[UUID] = set()
        external_ids: set[tuple[str, str]] = set()
        for record in response.items:
            external_identity = (record.source, record.change.external_change_id)
            if record.record_id in record_ids or external_identity in external_ids:
                raise InvestigationDataSourceFailure(
                    error_code="change_duplicate_record",
                    message="变更调查响应包含重复记录",
                )
            record_ids.add(record.record_id)
            external_ids.add(external_identity)
            if not earliest <= record.occurred_at <= response.generated_at:
                raise InvestigationDataSourceFailure(
                    error_code="change_invalid_timestamp",
                    message="变更记录发生时间不在请求窗口内",
                )
            if record.recorded_at > response.generated_at + timedelta(
                seconds=MAX_FUTURE_CLOCK_SKEW_SECONDS
            ):
                raise InvestigationDataSourceFailure(
                    error_code="change_invalid_timestamp",
                    message="变更记录入库时间晚于查询生成时间",
                )

        deployment_records = tuple(
            record for record in response.items if isinstance(record.change, DeploymentChange)
        )
        ordered_deployments = tuple(
            sorted(
                deployment_records,
                key=lambda record: (record.occurred_at, record.recorded_at, record.record_id),
            )
        )
        current_deployment = ordered_deployments[-1] if ordered_deployments else None
        previous_deployment = ordered_deployments[-2] if len(ordered_deployments) >= 2 else None
        previous_artifact = (
            previous_deployment.change.artifact_reference
            if previous_deployment is not None
            and isinstance(previous_deployment.change, DeploymentChange)
            and previous_deployment.change.completed
            else None
        )
        runtime_inventory = None
        if (
            current_deployment is not None
            and isinstance(current_deployment.change, DeploymentChange)
            and current_deployment.change.artifact_reference is not None
        ):
            runtime_inventory = self._get_runtime_inventory(request_time=request_time)

        observations = tuple(
            self._to_evidence(
                record,
                runtime_inventory=runtime_inventory,
                previous_artifact=previous_artifact,
                is_current_deployment=(
                    current_deployment is not None
                    and record.record_id == current_deployment.record_id
                ),
            )
            for record in response.items
        )
        return tuple(sorted(observations, key=lambda item: (item.observed_at, item.evidence_id)))

    def _get_changes(self, *, params: dict[str, str | int]) -> bytes:
        try:
            with httpx2.Client(
                base_url=self._settings.change_base_url,
                timeout=self._settings.request_timeout_seconds,
                transport=self._transport,
                trust_env=False,
            ) as client:
                response = client.get("/api/v1/changes", params=params)
        except httpx2.TimeoutException as error:
            raise InvestigationDataSourceFailure(
                error_code="change_query_timeout",
                message="变更调查查询超时",
            ) from error
        except httpx2.HTTPError as error:
            raise InvestigationDataSourceFailure(
                error_code="change_query_unavailable",
                message="变更调查查询不可用",
            ) from error
        if not 200 <= response.status_code < 300:
            raise InvestigationDataSourceFailure(
                error_code="change_unexpected_status",
                message="变更调查查询返回了非预期状态",
                http_status=response.status_code,
            )
        payload = response.content
        if len(payload) > MAX_RESPONSE_BYTES:
            raise InvestigationDataSourceFailure(
                error_code="change_response_too_large",
                message="变更调查响应超过允许大小",
            )
        return payload

    def _get_runtime_inventory(
        self,
        *,
        request_time: datetime,
    ) -> RuntimeInventoryResponse | None:
        try:
            with httpx2.Client(
                base_url=self._settings.runtime_base_url,
                timeout=self._settings.request_timeout_seconds,
                transport=self._runtime_transport,
                trust_env=False,
            ) as client:
                response = client.get("/api/v1/runtime-inventory")
        except httpx2.TimeoutException as error:
            raise InvestigationDataSourceFailure(
                error_code="runtime_inventory_timeout",
                message="运行清单查询超时",
            ) from error
        except httpx2.HTTPError as error:
            raise InvestigationDataSourceFailure(
                error_code="runtime_inventory_unavailable",
                message="运行清单查询不可用",
            ) from error
        payload = response.content
        if len(payload) > MAX_RUNTIME_INVENTORY_RESPONSE_BYTES:
            raise InvestigationDataSourceFailure(
                error_code="runtime_inventory_response_too_large",
                message="运行清单响应超过允许大小",
            )
        if response.status_code == 503:
            try:
                RuntimeInventoryUnavailableResponse.model_validate_json(payload)
            except (ValidationError, ValueError) as error:
                raise InvestigationDataSourceFailure(
                    error_code="runtime_inventory_invalid_response",
                    message="运行清单禁用响应不符合精确契约",
                ) from error
            return None
        if not 200 <= response.status_code < 300:
            raise InvestigationDataSourceFailure(
                error_code="runtime_inventory_unexpected_status",
                message="运行清单查询返回了非预期状态",
                http_status=response.status_code,
            )
        try:
            inventory = RuntimeInventoryResponse.model_validate_json(payload)
        except (ValidationError, ValueError) as error:
            raise InvestigationDataSourceFailure(
                error_code="runtime_inventory_invalid_response",
                message="运行清单响应不符合精确契约",
            ) from error
        difference = (inventory.reported_at - request_time).total_seconds()
        if difference > MAX_FUTURE_CLOCK_SKEW_SECONDS or difference < -MAX_RESPONSE_AGE_SECONDS:
            raise InvestigationDataSourceFailure(
                error_code="runtime_inventory_stale_response",
                message="运行清单报告时间异常或已经过期",
            )
        return inventory

    @staticmethod
    def _validate_response_time(
        response: ChangeRecordListResponse,
        *,
        request_time: datetime,
    ) -> None:
        difference = (response.generated_at - request_time).total_seconds()
        if difference > MAX_FUTURE_CLOCK_SKEW_SECONDS or difference < -MAX_RESPONSE_AGE_SECONDS:
            raise InvestigationDataSourceFailure(
                error_code="change_stale_response",
                message="变更调查响应生成时间异常或已经过期",
            )

    @staticmethod
    def _assess_runtime_inventory(
        *,
        declared_artifact: DeploymentArtifactReference,
        previous_artifact: DeploymentArtifactReference | None,
        inventory: RuntimeInventoryResponse,
    ) -> RuntimeArtifactAssessment:
        instance_results: list[JsonValue] = []
        matching_count = 0
        previous_count = 0
        unexpected_count = 0
        for instance in inventory.instances:
            if ChangeApiOnlyInvestigationDataSource._matches_artifact(
                instance,
                declared_artifact,
            ):
                classification = "target"
                matching_count += 1
            elif previous_artifact is not None and (
                ChangeApiOnlyInvestigationDataSource._matches_artifact(
                    instance,
                    previous_artifact,
                )
            ):
                classification = "previous"
                previous_count += 1
            else:
                classification = "unexpected"
                unexpected_count += 1
            instance_result: dict[str, JsonValue] = {
                "instance_id": str(instance.instance_id),
                "image_repository": instance.image_repository,
                "image_digest": instance.image_digest,
                "started_at": instance.started_at.isoformat(),
                "classification": classification,
            }
            instance_results.append(instance_result)

        instance_count = len(inventory.instances)
        rollout = inventory.rollout
        rollout_target_matches = (
            rollout.target_image_repository == declared_artifact.image_repository
            and rollout.target_image_digest == declared_artifact.image_digest
        )
        rollout_window_active = (
            rollout.phase == "progressing"
            and rollout.started_at is not None
            and rollout.deadline_at is not None
            and rollout.started_at <= inventory.reported_at <= rollout.deadline_at
        )
        if matching_count == instance_count:
            status: RuntimeArtifactStatus = "matched"
        elif (
            matching_count > 0
            and previous_count > 0
            and unexpected_count == 0
            and rollout_target_matches
            and rollout_window_active
        ):
            status = "rolling_update"
        elif matching_count > 0:
            status = "partial_mismatch"
        else:
            status = "mismatched"
        return RuntimeArtifactAssessment(
            status=status,
            matching_count=matching_count,
            previous_count=previous_count,
            unexpected_count=unexpected_count,
            instances=instance_results,
        )

    @staticmethod
    def _matches_artifact(
        instance: RuntimeArtifactInstance,
        artifact: DeploymentArtifactReference,
    ) -> bool:
        return (
            instance.image_repository == artifact.image_repository
            and instance.image_digest == artifact.image_digest
        )

    @staticmethod
    def _to_evidence(
        record: ChangeRecord,
        *,
        runtime_inventory: RuntimeInventoryResponse | None,
        previous_artifact: DeploymentArtifactReference | None,
        is_current_deployment: bool,
    ) -> EvidenceObservation:
        change = record.change
        common_attributes: dict[str, JsonValue] = {
            "change_type": change.change_type,
            "external_change_id": change.external_change_id,
            "completed": change.completed,
            "record_source": record.source,
            "source_identity": record.source_identity,
            "source_contract_version": record.source_contract_version,
        }
        if isinstance(change, DeploymentChange):
            attributes: dict[str, JsonValue] = {
                **common_attributes,
                "from_version": change.from_version,
                "to_version": change.to_version,
                "code_reference_status": (
                    "declared" if change.code_reference is not None else "not_declared"
                ),
                "artifact_reference_status": (
                    "declared" if change.artifact_reference is not None else "not_declared"
                ),
                "deployment_position": "current" if is_current_deployment else "historical",
                "runtime_artifact_status": (
                    "unavailable"
                    if change.artifact_reference is not None and is_current_deployment
                    else "historical"
                    if change.artifact_reference is not None
                    else "not_declared"
                ),
            }
            summary = f"order-service 已从 {change.from_version} 发布为 {change.to_version}。"
            if change.code_reference is not None:
                attributes.update(
                    repository=change.code_reference.repository,
                    commit_sha=change.code_reference.commit_sha,
                )
                summary += (
                    f" 部署声明关联 {change.code_reference.repository} 的提交 "
                    f"{change.code_reference.commit_sha[:7]}。"
                )
            if change.artifact_reference is not None:
                attributes.update(
                    image_repository=change.artifact_reference.image_repository,
                    image_digest=change.artifact_reference.image_digest,
                )
                summary += (
                    f" 构建产物为 {change.artifact_reference.image_repository}@"
                    f"{change.artifact_reference.image_digest[:19]}…。"
                )
                if not is_current_deployment:
                    summary += " 这是历史部署事实，不与当前运行实例比较。"
                elif runtime_inventory is not None:
                    assessment = ChangeApiOnlyInvestigationDataSource._assess_runtime_inventory(
                        declared_artifact=change.artifact_reference,
                        previous_artifact=previous_artifact,
                        inventory=runtime_inventory,
                    )
                    attributes.update(
                        runtime_artifact_status=assessment.status,
                        runtime_instance_count=len(runtime_inventory.instances),
                        runtime_matching_instance_count=assessment.matching_count,
                        runtime_previous_instance_count=assessment.previous_count,
                        runtime_unexpected_instance_count=assessment.unexpected_count,
                        runtime_instances=assessment.instances,
                        runtime_rollout_phase=runtime_inventory.rollout.phase,
                        runtime_inventory_reported_at=runtime_inventory.reported_at.isoformat(),
                        runtime_identity_source=runtime_inventory.identity_source,
                        runtime_proof_status=(
                            "verified" if runtime_inventory.proof is not None else "unsigned"
                        ),
                    )
                    if runtime_inventory.proof is not None:
                        attributes.update(
                            runtime_proof_id=str(runtime_inventory.proof.proof_id),
                            runtime_proof_key_id=runtime_inventory.proof.key_id,
                            runtime_proof_algorithm=runtime_inventory.proof.algorithm,
                            runtime_proof_issued_at=(runtime_inventory.proof.issued_at.isoformat()),
                            runtime_proof_expires_at=(
                                runtime_inventory.proof.expires_at.isoformat()
                            ),
                        )
                    if previous_artifact is not None:
                        attributes.update(
                            runtime_previous_image_repository=(previous_artifact.image_repository),
                            runtime_previous_image_digest=previous_artifact.image_digest,
                        )
                    rollout = runtime_inventory.rollout
                    if rollout.phase == "progressing":
                        assert rollout.rollout_id is not None
                        assert rollout.started_at is not None
                        assert rollout.deadline_at is not None
                        assert rollout.target_image_repository is not None
                        assert rollout.target_image_digest is not None
                        attributes.update(
                            runtime_rollout_id=str(rollout.rollout_id),
                            runtime_rollout_started_at=rollout.started_at.isoformat(),
                            runtime_rollout_deadline_at=rollout.deadline_at.isoformat(),
                            runtime_rollout_target_image_repository=(
                                rollout.target_image_repository
                            ),
                            runtime_rollout_target_image_digest=rollout.target_image_digest,
                        )
                    if assessment.status == "matched":
                        summary += f" {assessment.matching_count} 个运行实例均与部署产物一致。"
                    elif assessment.status == "rolling_update":
                        summary += (
                            f" 滚动发布进行中：{assessment.matching_count} 个目标实例、"
                            f"{assessment.previous_count} 个上一版本实例。"
                        )
                    elif assessment.status == "partial_mismatch":
                        summary += (
                            f" 仅 {assessment.matching_count}/{len(runtime_inventory.instances)} "
                            "个运行实例与部署产物一致。"
                        )
                    else:
                        summary += " 所有运行实例均与该部署产物不一致。"
                    if runtime_inventory.proof is not None:
                        summary += (
                            f" 运行清单已通过 {runtime_inventory.proof.key_id} 的 "
                            "Ed25519 签名验证。"
                        )
                    else:
                        summary += " 运行清单来自未签名的部署环境声明。"
                else:
                    summary += " 当前运行清单未启用，无法核对实例集合。"
        elif isinstance(change, ConfigurationChange):
            attributes = {
                **common_attributes,
                "key": change.key,
                "from_value": change.from_value,
                "to_value": change.to_value,
            }
            summary = (
                "order-service 已将数据库连接池上限从 "
                f"{change.from_value} 调整为 {change.to_value}。"
            )
        elif isinstance(change, DependencyMaintenanceChange):
            attributes = {
                **common_attributes,
                "dependency": change.dependency,
                "operation": change.operation,
                "order_release_changed": change.order_release_changed,
            }
            summary = "Redis 已执行维护重启，order-service 版本未发生变化。"
        else:
            raise AssertionError("变更记录类型已由 Pydantic 完整校验")
        return EvidenceObservation(
            evidence_id=f"change-{record.record_id}",
            kind=EvidenceKind.CHANGE,
            source="change-simulator",
            summary=summary,
            observed_at=record.occurred_at,
            attributes=attributes,
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
