"""身份测试使用公开固定的非生产凭据，不读取用户认证环境。"""

import hashlib
from datetime import timedelta

from opsswarm.identity.models import OperatorRole
from opsswarm.identity.settings import OperatorAuthSettings, OperatorCredential
from tests.api.test_control_plane_api import BASE_TIME


def token(role: OperatorRole | str) -> str:
    name = str(role)
    return f"{name}.{name.replace('_', '-') * 40}"[: len(name) + 1 + 96]


def auth_settings() -> OperatorAuthSettings:
    return OperatorAuthSettings(
        mode="bearer",
        credentials=tuple(
            OperatorCredential(
                credential_id=role.value,
                subject_id=f"trusted-{role.value}",
                role=role,
                secret_sha256=hashlib.sha256(token(role).split(".")[1].encode()).hexdigest(),
                expires_at=BASE_TIME + timedelta(days=1),
            )
            for role in OperatorRole
        ),
    )


def auth_header(role: OperatorRole | str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token(role)}"}
