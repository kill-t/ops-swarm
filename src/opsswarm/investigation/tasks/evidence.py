"""专业取证材料的范围、完整性与不可变内容契约。"""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, Field, field_validator, model_validator

from opsswarm.investigation.models import EvidenceKind, EvidenceObservation
from opsswarm.investigation.tasks.contracts import TaskAssignment, TaskModel
from opsswarm.investigation.tasks.errors import TaskFactConflict

EvidencePolicyVersion = Literal["task-evidence-v1", "task-evidence-live-v1"]


def live_snapshot_id(run_id: UUID, cutoff: datetime) -> str:
    return f"live-v1:{run_id}:{cutoff.astimezone(UTC).isoformat()}"


class TaskEvidenceSnapshot(TaskModel):
    version: EvidencePolicyVersion = "task-evidence-v1"
    data_source_mode: Literal["frozen", "live"] = "frozen"
    evidence_snapshot_id: str = Field(min_length=1, max_length=100)
    role: EvidenceKind
    service_name: str = Field(min_length=1, max_length=100)
    window_start: AwareDatetime
    window_end: AwareDatetime
    evidence: tuple[EvidenceObservation, ...] = Field(max_length=20)

    @field_validator("window_start", "window_end")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_material(self) -> Self:
        if (self.version == "task-evidence-live-v1") != (self.data_source_mode == "live"):
            raise ValueError("取证策略与数据来源模式不一致")
        if not timedelta(0) < self.window_end - self.window_start <= timedelta(days=1):
            raise ValueError("取证时间窗必须为正且不超过一天")
        if len({e.evidence_id for e in self.evidence}) != len(self.evidence):
            raise ValueError("取证快照不能包含重复证据 ID")
        if any(
            e.kind is not self.role
            or not self.window_start <= e.observed_at <= self.window_end
            or not e.source.strip()
            for e in self.evidence
        ):
            raise ValueError("证据角色、时间窗或来源不符合取证范围")
        if self.data_source_mode == "live":
            expected_source = {
                EvidenceKind.LOG: "loki-fault-lab",
                EvidenceKind.METRIC: "prometheus-fault-lab",
                EvidenceKind.CHANGE: "change-simulator",
            }[self.role]
            if any(
                e.source != expected_source
                or e.attributes.get("service_name") != self.service_name
                or e.attributes.get("window_start") != self.window_start.isoformat()
                or e.attributes.get("window_end") != self.window_end.isoformat()
                for e in self.evidence
            ):
                raise ValueError("现场材料缺少匹配的来源、服务或窗口信息")
        if len(_canonical(self)) > 131072:
            raise ValueError("取证快照不能超过 128 KiB")
        return self


def _canonical(snapshot: TaskEvidenceSnapshot) -> bytes:
    return json.dumps(
        snapshot.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def evidence_hash(snapshot: TaskEvidenceSnapshot) -> str:
    return hashlib.sha256(_canonical(snapshot)).hexdigest()


class TaskEvidenceRecord(TaskModel):
    task_id: UUID
    investigation_run_id: UUID
    attempt_id: UUID
    call_id: UUID
    snapshot: TaskEvidenceSnapshot
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: AwareDatetime

    @model_validator(mode="after")
    def validate_hash(self) -> Self:
        if evidence_hash(self.snapshot) != self.content_sha256:
            raise ValueError("取证快照内容与散列不一致")
        return self


def require_matching_assignment(snapshot: TaskEvidenceSnapshot, assignment: TaskAssignment) -> None:
    if (
        snapshot.role is not assignment.role
        or snapshot.service_name != assignment.service_name
        or snapshot.window_start != assignment.window_start
        or snapshot.window_end != assignment.window_end
        or snapshot.evidence_snapshot_id != assignment.evidence_snapshot_id
    ):
        raise TaskFactConflict("task_evidence_scope_mismatch", "取证快照与当前任务范围不一致")
