"""在独立事务持久化授权准入，失败时不允许调用业务服务。"""

import hashlib
from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opsswarm.identity.models import OperatorPrincipal, require_permission
from opsswarm.identity.tables import OperatorAuthorizationRow


class OperatorAuthorizationStore:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = session_factory

    async def record(
        self,
        principal: OperatorPrincipal,
        *,
        operation: str,
        method: str,
        route: str,
        resources: dict[str, str],
        body: bytes,
        authorized_at: datetime,
    ) -> UUID:
        require_permission(principal, method, operation)
        authorization_id = uuid4()
        async with self._factory() as session, session.begin():
            session.add(
                OperatorAuthorizationRow(
                    id=authorization_id,
                    policy_version="operator-auth-v1",
                    subject_id=principal.subject_id,
                    credential_id=principal.credential_id,
                    role=principal.role.value,
                    permissions=[item.value for item in principal.permissions],
                    operation=operation,
                    method=method,
                    route=route,
                    resources=resources,
                    body_sha256=hashlib.sha256(body).hexdigest(),
                    authorized_at=authorized_at,
                )
            )
        return authorization_id
