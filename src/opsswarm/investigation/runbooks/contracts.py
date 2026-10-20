"""调查手册的元数据、目录、读取参数与有界正文契约。"""

import hashlib
import json
from typing import Annotated, Literal, Self
from uuid import UUID, uuid5

from pydantic import ConfigDict, Field, model_validator

from opsswarm.investigation.models import EvidenceKind
from opsswarm.investigation.tasks.contracts import TaskModel

MAX_RUNBOOK_BYTES = 8 * 1024
RUNBOOK_NAMESPACE = UUID("8a57bb70-7e85-5da5-8b3b-1478d0a8ca80")

RunbookId = Annotated[
    str,
    Field(min_length=1, max_length=100, pattern=r"^[a-z0-9][a-z0-9-]*$"),
]
RunbookVersion = Annotated[str, Field(pattern=r"^v[1-9][0-9]*$", max_length=20)]
ContentHash = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
RunbookSelectorVersion = Literal["order-service-recent-completed-deployment-v1"]
RunbookCatalogVersion = Literal["investigation-runbook-catalog-v1"]


def runbook_material_id(runbook_id: str, version: str, content_sha256: str) -> UUID:
    """材料身份同时绑定逻辑版本与正文，版本误改也不会产生身份碰撞。"""

    return uuid5(RUNBOOK_NAMESPACE, f"runbook:{runbook_id}:{version}:{content_sha256}")


def _catalog_hash(entries: tuple["RunbookCatalogEntry", ...]) -> str:
    payload = json.dumps(
        [entry.model_dump(mode="json") for entry in entries],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def catalog_material_id(version: str, content_sha256: str) -> UUID:
    """目录材料身份绑定目录策略和精确内容。"""

    return uuid5(RUNBOOK_NAMESPACE, f"catalog:{version}:{content_sha256}")


class RunbookMetadata(TaskModel):
    """与 Markdown 一同受 Git 管理的严格发布元数据。"""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=False)

    schema_version: Literal["runbook-metadata-v1"] = "runbook-metadata-v1"
    runbook_id: RunbookId
    version: RunbookVersion
    title: str = Field(min_length=1, max_length=100)
    summary: str = Field(min_length=1, max_length=300)
    locale: Literal["zh-CN"] = "zh-CN"
    authority: Literal["guidance_only"] = "guidance_only"
    service_name: str = Field(min_length=1, max_length=100)
    role: EvidenceKind
    selector_version: RunbookSelectorVersion
    content_file: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*\.md$", max_length=120)
    content_sha256: ContentHash
    content_bytes: int = Field(ge=1, le=MAX_RUNBOOK_BYTES, strict=True)

    @model_validator(mode="after")
    def validate_fixed_selector_scope(self) -> Self:
        if self.selector_version == "order-service-recent-completed-deployment-v1" and (
            self.service_name != "order-service" or self.role is not EvidenceKind.CHANGE
        ):
            raise ValueError("发布相关性选择器只允许 order-service 的变更角色")
        return self


class RunbookCatalogEntry(TaskModel):
    """模型可见的已授权手册摘要，不泄露包路径和选择实现。"""

    material_id: UUID
    runbook_id: RunbookId
    version: RunbookVersion
    title: str = Field(min_length=1, max_length=100)
    summary: str = Field(min_length=1, max_length=300)
    content_sha256: ContentHash
    content_bytes: int = Field(ge=1, le=MAX_RUNBOOK_BYTES, strict=True)
    authority: Literal["guidance_only"] = "guidance_only"

    @model_validator(mode="after")
    def validate_material_id(self) -> Self:
        if self.material_id != runbook_material_id(
            self.runbook_id, self.version, self.content_sha256
        ):
            raise ValueError("手册材料身份与版本不一致")
        return self


class RunbookCatalog(TaskModel):
    """一次任务确定性选择出的有界手册目录。"""

    version: RunbookCatalogVersion = "investigation-runbook-catalog-v1"
    material_id: UUID
    content_sha256: ContentHash
    entries: tuple[RunbookCatalogEntry, ...] = Field(max_length=3)

    @model_validator(mode="after")
    def validate_catalog_identity(self) -> Self:
        if tuple(sorted(self.entries, key=lambda item: (item.runbook_id, item.version))) != (
            self.entries
        ):
            raise ValueError("手册目录必须按身份和版本稳定排序")
        if len({(item.runbook_id, item.version) for item in self.entries}) != len(self.entries):
            raise ValueError("手册目录不能包含重复版本")
        expected_hash = _catalog_hash(self.entries)
        if self.content_sha256 != expected_hash or self.material_id != catalog_material_id(
            self.version, expected_hash
        ):
            raise ValueError("手册目录材料身份或散列不一致")
        return self


class ReadRunbookArguments(TaskModel):
    """模型只能选择已注入目录中的不可变身份，不能传入路径或任务范围。"""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=False)

    runbook_id: RunbookId
    version: RunbookVersion
    content_sha256: ContentHash


class RunbookDocument(TaskModel):
    """读取工具返回的有界指导材料；它不是事故证据或执行授权。"""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=False)

    material_id: UUID
    runbook_id: RunbookId
    version: RunbookVersion
    title: str = Field(min_length=1, max_length=100)
    content_sha256: ContentHash
    content_bytes: int = Field(ge=1, le=MAX_RUNBOOK_BYTES, strict=True)
    content: str = Field(min_length=1, max_length=MAX_RUNBOOK_BYTES)
    authority: Literal["guidance_only"] = "guidance_only"

    @model_validator(mode="after")
    def validate_content_identity(self) -> Self:
        payload = self.content.encode("utf-8")
        if (
            self.material_id
            != runbook_material_id(self.runbook_id, self.version, self.content_sha256)
            or self.content_bytes != len(payload)
            or self.content_sha256 != hashlib.sha256(payload).hexdigest()
        ):
            raise ValueError("手册正文身份、长度或散列不一致")
        return self


class RunbookReadDetail(TaskModel):
    """应用层把精确正文绑定到系统注入的运行、模型调用和专业任务。"""

    investigation_run_id: UUID
    call_id: UUID
    task_id: UUID
    document: RunbookDocument
