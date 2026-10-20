"""写命令的授权准入快照；不把准入记录当作业务成功。"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, Index, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column

from opsswarm.database.base import Base


class OperatorAuthorizationRow(Base):
    __tablename__ = "operator_authorizations"
    __table_args__ = (
        CheckConstraint("policy_version = 'operator-auth-v1'", name="ck_operator_auth_policy"),
        CheckConstraint("method = 'POST'", name="ck_operator_auth_method"),
        CheckConstraint(
            "role IN ('viewer', 'engineer', 'approver', 'executor', 'alert_source')",
            name="ck_operator_auth_role",
        ),
        CheckConstraint("body_sha256 ~ '^[0-9a-f]{64}$'", name="ck_operator_auth_body_hash"),
        CheckConstraint("jsonb_typeof(permissions) = 'array'", name="ck_operator_auth_permissions"),
        CheckConstraint("jsonb_typeof(resources) = 'object'", name="ck_operator_auth_resources"),
        Index("ix_operator_auth_subject_time", "subject_id", "authorized_at"),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    policy_version: Mapped[str] = mapped_column(String(32), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(100), nullable=False)
    credential_id: Mapped[str] = mapped_column(String(64), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    permissions: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    operation: Mapped[str] = mapped_column(String(80), nullable=False)
    method: Mapped[str] = mapped_column(String(8), nullable=False)
    route: Mapped[str] = mapped_column(String(256), nullable=False)
    resources: Mapped[dict[str, str]] = mapped_column(JSONB, nullable=False)
    body_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    authorized_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
