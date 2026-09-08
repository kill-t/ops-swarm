"""证据目录与详情契约，保留完整摘要和版本定位。"""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, Field

from opsswarm.investigation.context.contracts import ContextPolicyVersion
from opsswarm.investigation.models import EvidenceKind, EvidenceObservation
from opsswarm.investigation.tasks.contracts import TaskModel
from opsswarm.investigation.tasks.evidence import EvidencePolicyVersion

SnapshotHash = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
EvidenceId = Annotated[str, Field(min_length=1, max_length=100, pattern=r"^[a-z0-9][a-z0-9-]*$")]


class EvidenceCatalogEntry(TaskModel):
    evidence_id: EvidenceId
    kind: EvidenceKind
    source: str = Field(min_length=1, max_length=100)
    summary: str = Field(min_length=1, max_length=500)
    observed_at: AwareDatetime


class EvidenceCatalogGroup(TaskModel):
    task_id: UUID
    version: EvidencePolicyVersion
    snapshot_id: str = Field(min_length=1, max_length=100)
    snapshot_sha256: SnapshotHash
    data_source_mode: Literal["frozen", "live"]
    service_name: str = Field(min_length=1, max_length=100)
    role: EvidenceKind
    window_start: AwareDatetime
    window_end: AwareDatetime
    entries: tuple[EvidenceCatalogEntry, ...] = Field(max_length=20)


class EvidenceCatalog(TaskModel):
    investigation_run_id: UUID
    call_id: UUID
    context_policy_version: ContextPolicyVersion
    groups: tuple[EvidenceCatalogGroup, ...] = Field(min_length=1, max_length=3)


class EvidenceReadRequest(TaskModel):
    """只选择目录内材料，调用归属由服务构造方绑定。"""

    task_id: UUID
    evidence_id: EvidenceId
    snapshot_sha256: SnapshotHash


class ContextEvidenceDetail(TaskModel):
    investigation_run_id: UUID
    call_id: UUID
    task_id: UUID
    version: EvidencePolicyVersion
    snapshot_id: str
    snapshot_sha256: SnapshotHash
    evidence: EvidenceObservation
