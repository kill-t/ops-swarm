"""固定格式高熵令牌的本地摘要验证，不接受客户端声明角色。"""

import hashlib
import hmac
import re
from datetime import datetime

from opsswarm.identity.models import OperatorAccessError, OperatorPrincipal
from opsswarm.identity.settings import OperatorAuthSettings

TOKEN_PATTERN = re.compile(r"([a-z0-9][a-z0-9_-]{0,63})\.([A-Za-z0-9_-]{32,128})", re.ASCII)


def authenticate_operator(
    settings: OperatorAuthSettings,
    authorization: str | None,
    now: datetime,
) -> OperatorPrincipal:
    """未知、错误、过期及兼容模式统一拒绝，避免暴露凭据是否存在。"""

    failure = OperatorAccessError("operator_unauthenticated", "需要有效操作者认证")
    if settings.mode != "bearer" or not authorization or len(authorization) > 220:
        raise failure
    scheme, separator, value = authorization.partition(" ")
    match = TOKEN_PATTERN.fullmatch(value)
    if not separator or scheme.lower() != "bearer" or match is None:
        raise failure
    credential_id, secret = match.groups()
    credential = next(
        (item for item in settings.credentials if item.credential_id == credential_id), None
    )
    actual = hashlib.sha256(secret.encode("ascii")).hexdigest()
    expected = credential.secret_sha256 if credential is not None else "0" * 64
    matches = hmac.compare_digest(actual, expected)
    if not matches or credential is None or now >= credential.expires_at:
        raise failure
    return OperatorPrincipal(
        subject_id=credential.subject_id,
        credential_id=credential.credential_id,
        role=credential.role,
        expires_at=credential.expires_at,
    )
