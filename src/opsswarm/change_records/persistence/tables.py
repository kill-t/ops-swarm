"""变更记录 SQLAlchemy 映射。"""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Index,
    SmallInteger,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column

from opsswarm.database.base import Base


class ChangeRecordRow(Base):
    """只追加、不提供更新与删除接口的外部变更事实。"""

    __tablename__ = "change_records"
    __table_args__ = (
        UniqueConstraint(
            "source",
            "external_change_id",
            name="uq_change_records_source_external_id",
        ),
        CheckConstraint(
            "source IN ('fault-lab-change-simulator', 'ci-deployment-ingestion')",
            name="ck_change_records_source",
        ),
        CheckConstraint(
            "(source = 'fault-lab-change-simulator' AND source_identity = 'local-admin') OR "
            "(source = 'ci-deployment-ingestion' AND source_identity <> 'local-admin' AND "
            "source_identity ~ '^[a-z0-9][a-z0-9._-]{0,63}$')",
            name="ck_change_records_source_identity",
        ),
        CheckConstraint(
            "source <> 'ci-deployment-ingestion' OR "
            "(change_type = 'deployment' AND details ? 'code_reference')",
            name="ck_change_records_ci_deployment",
        ),
        CheckConstraint(
            "(source = 'fault-lab-change-simulator' AND source_contract_version = 1 AND "
            "NOT (details ? 'artifact_reference')) OR "
            "(source = 'ci-deployment-ingestion' AND "
            "((source_contract_version = 1 AND NOT (details ? 'artifact_reference')) OR "
            "source_contract_version = 2))",
            name="ck_change_records_source_contract",
        ),
        CheckConstraint(
            "source <> 'ci-deployment-ingestion' OR source_contract_version = 1 OR "
            "(source_contract_version = 2 AND change_type = 'deployment' AND "
            "details ? 'code_reference' AND details ? 'artifact_reference')",
            name="ck_change_records_ci_artifact_v2",
        ),
        CheckConstraint(
            "service_name = 'order-service'",
            name="ck_change_records_service",
        ),
        CheckConstraint(
            "change_type IN ('deployment', 'configuration', 'dependency_maintenance')",
            name="ck_change_records_type",
        ),
        CheckConstraint("jsonb_typeof(details) = 'object'", name="ck_change_records_details"),
        CheckConstraint(
            "(change_type = 'deployment' AND (NOT (details ? 'code_reference') OR ("
            "jsonb_typeof(details -> 'code_reference') = 'object' AND "
            "jsonb_typeof(details #> '{code_reference,repository}') = 'string' AND "
            "jsonb_typeof(details #> '{code_reference,commit_sha}') = 'string' AND "
            "length(details #>> '{code_reference,repository}') BETWEEN 3 AND 140 AND "
            "(details #>> '{code_reference,repository}') "
            "~ '^[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}$' AND "
            "split_part(details #>> '{code_reference,repository}', '/', 2) "
            "NOT IN ('.', '..') AND "
            "(details #>> '{code_reference,commit_sha}') ~ '^[0-9a-f]{40,64}$'"
            "))) OR (change_type <> 'deployment' AND NOT (details ? 'code_reference'))",
            name="ck_change_records_code_reference",
        ),
        CheckConstraint(
            "(change_type = 'deployment' AND (NOT (details ? 'artifact_reference') OR ("
            "jsonb_typeof(details -> 'artifact_reference') = 'object' AND "
            "(details -> 'artifact_reference') - 'image_repository' - 'image_digest' "
            "= '{}'::jsonb AND "
            "jsonb_typeof(details #> '{artifact_reference,image_repository}') = 'string' AND "
            "jsonb_typeof(details #> '{artifact_reference,image_digest}') = 'string' AND "
            "length(details #>> '{artifact_reference,image_repository}') BETWEEN 3 AND 255 AND "
            "(details #>> '{artifact_reference,image_repository}') "
            "~ '^[a-z0-9][a-z0-9._-]*"
            "(?::([1-9][0-9]{0,3}|[1-5][0-9]{4}|6[0-4][0-9]{3}|"
            "65[0-4][0-9]{2}|655[0-2][0-9]|6553[0-5]))?"
            "(/[a-z0-9][a-z0-9._-]*)+$' AND "
            "(details #>> '{artifact_reference,image_digest}') ~ '^sha256:[0-9a-f]{64}$'"
            "))) OR (change_type <> 'deployment' AND NOT (details ? 'artifact_reference'))",
            name="ck_change_records_artifact_reference",
        ),
        CheckConstraint(
            "content_hash ~ '^[0-9a-f]{64}$'",
            name="ck_change_records_content_hash",
        ),
        CheckConstraint(
            "occurred_at <= recorded_at",
            name="ck_change_records_timestamp_order",
        ),
        Index(
            "ix_change_records_service_occurred",
            "service_name",
            text("occurred_at DESC"),
            text("id DESC"),
        ),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    source: Mapped[str] = mapped_column(String(100), nullable=False)
    source_identity: Mapped[str] = mapped_column(String(64), nullable=False)
    source_contract_version: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    external_change_id: Mapped[str] = mapped_column(String(100), nullable=False)
    service_name: Mapped[str] = mapped_column(String(100), nullable=False)
    change_type: Mapped[str] = mapped_column(String(32), nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
