"""本地部署控制器 API。"""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID, uuid4

from fastapi import FastAPI, Response
from pydantic import BaseModel, ConfigDict

from opsswarm import __version__
from opsswarm.deployment_controller.settings import DeploymentControllerSettings
from opsswarm.fault_lab.runtime_metadata import (
    RuntimeArtifactInstance,
    RuntimeInventoryResponse,
)
from opsswarm.fault_lab.runtime_proof import (
    RuntimeInventoryProofEnvelope,
    RuntimeInventoryProofPayload,
    issue_runtime_inventory_proof,
)

Clock = Callable[[], datetime]
ProofIdFactory = Callable[[], UUID]


def utc_now() -> datetime:
    """返回带时区的当前 UTC 时间。"""

    return datetime.now(UTC)


class ControllerHealthResponse(BaseModel):
    """控制器健康状态不包含任何密钥信息。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    service: Literal["deployment-controller"] = "deployment-controller"
    status: Literal["healthy"] = "healthy"


def _normalize_clock(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("部署控制器时钟必须包含时区")
    return value.astimezone(UTC)


def create_app(
    settings: DeploymentControllerSettings | None = None,
    *,
    clock: Clock = utc_now,
    proof_id_factory: ProofIdFactory = uuid4,
) -> FastAPI:
    """创建只读签发 API；不接受请求参数或运行时身份。"""

    controller_settings = settings or DeploymentControllerSettings()
    identity = controller_settings.identity()
    declaration = controller_settings.declaration()
    private_key = controller_settings.load_private_key()
    controller_started_at = _normalize_clock(clock())
    instances = (
        RuntimeArtifactInstance(
            **identity.model_dump(),
            started_at=controller_started_at,
        ),
        *declaration.additional_instances,
    )
    RuntimeInventoryResponse(
        identity_source="deployment-environment",
        instances=instances,
        rollout=declaration.rollout,
        reported_at=controller_started_at,
    )

    app = FastAPI(
        title="OpsSwarm Deployment Controller",
        version=__version__,
        description="本地演练环境的 Ed25519 短时运行清单证明签发器。",
    )

    @app.get("/health", response_model=ControllerHealthResponse, tags=["系统"])
    def get_health() -> ControllerHealthResponse:
        return ControllerHealthResponse()

    @app.get(
        "/api/v1/runtime-inventory-proof",
        response_model=RuntimeInventoryProofEnvelope,
        tags=["运行证明"],
    )
    def get_runtime_inventory_proof(response: Response) -> RuntimeInventoryProofEnvelope:
        """每次请求签发新证明，不返回公钥或私钥。"""

        issued_at = _normalize_clock(clock())
        payload = RuntimeInventoryProofPayload(
            proof_id=proof_id_factory(),
            issued_at=issued_at,
            expires_at=issued_at + timedelta(seconds=controller_settings.proof_lifetime_seconds),
            instances=instances,
            rollout=declaration.rollout,
        )
        response.headers["Cache-Control"] = "no-store"
        return issue_runtime_inventory_proof(
            payload,
            key_id=controller_settings.key_id,
            private_key=private_key,
        )

    return app
