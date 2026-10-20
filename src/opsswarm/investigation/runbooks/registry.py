"""从固定包目录校验、选择并读取不可变调查手册。"""

from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from pathlib import Path
from typing import Any, Literal

from pydantic import ValidationError

from opsswarm.investigation.models import EvidenceKind, InvestigationRequest
from opsswarm.investigation.runbooks.contracts import (
    MAX_RUNBOOK_BYTES,
    ReadRunbookArguments,
    RunbookCatalog,
    RunbookCatalogEntry,
    RunbookDocument,
    RunbookMetadata,
    catalog_material_id,
    runbook_material_id,
)
from opsswarm.investigation.tasks.evidence import TaskEvidenceSnapshot

MAX_METADATA_BYTES = 4 * 1024
PUBLISHED_METADATA_FILES = ("order-service-release-correlation-v1.json",)
REQUIRED_SECTIONS = (
    "适用范围",
    "调查目标",
    "必需材料",
    "调查步骤",
    "反证与竞争解释",
    "停止条件",
    "权限边界",
)

RunbookErrorCode = Literal[
    "runbook_catalog_corrupt",
    "runbook_content_corrupt",
    "runbook_not_found",
    "runbook_version_conflict",
]


class RunbookRegistryError(ValueError):
    """向上层提供稳定错误码，不暴露本地文件路径或解析细节。"""

    def __init__(self, code: RunbookErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


class RunbookRegistry:
    """仅加载代码固定的元数据文件，调用参数永远不能成为文件路径。"""

    def __init__(
        self,
        root: Path | None = None,
        metadata_files: tuple[str, ...] = PUBLISHED_METADATA_FILES,
    ) -> None:
        self._root = root or Path(__file__).with_name("content")
        self._metadata_files = metadata_files

    def catalog(
        self,
        request: InvestigationRequest,
        snapshot: TaskEvidenceSnapshot,
    ) -> RunbookCatalog:
        entries = tuple(
            sorted(
                (
                    self._entry(metadata)
                    for metadata in self._metadata()
                    if self._matches(metadata, request, snapshot)
                ),
                key=lambda item: (item.runbook_id, item.version),
            )
        )
        if len(entries) > 3:
            raise RunbookRegistryError("runbook_catalog_corrupt", "匹配的调查手册超过上限")
        content_sha256 = self._catalog_hash(entries)
        return RunbookCatalog(
            material_id=catalog_material_id("investigation-runbook-catalog-v1", content_sha256),
            content_sha256=content_sha256,
            entries=entries,
        )

    def read(self, catalog: RunbookCatalog, arguments: ReadRunbookArguments) -> RunbookDocument:
        matching_id = tuple(
            entry for entry in catalog.entries if entry.runbook_id == arguments.runbook_id
        )
        if not matching_id:
            raise RunbookRegistryError("runbook_not_found", "当前任务目录没有指定调查手册")
        entry = next(
            (item for item in matching_id if item.version == arguments.version),
            None,
        )
        if entry is None or entry.content_sha256 != arguments.content_sha256:
            raise RunbookRegistryError(
                "runbook_version_conflict", "调查手册版本或内容散列与目录不一致"
            )
        metadata = next(
            (
                item
                for item in self._metadata()
                if item.runbook_id == arguments.runbook_id and item.version == arguments.version
            ),
            None,
        )
        if metadata is None:
            raise RunbookRegistryError("runbook_content_corrupt", "目录引用的调查手册已缺失")
        content = self._content(metadata)
        if (
            metadata.title != entry.title
            or metadata.content_sha256 != entry.content_sha256
            or metadata.content_bytes != entry.content_bytes
        ):
            raise RunbookRegistryError("runbook_content_corrupt", "目录与调查手册元数据不一致")
        return RunbookDocument(
            material_id=entry.material_id,
            runbook_id=entry.runbook_id,
            version=entry.version,
            title=entry.title,
            content_sha256=entry.content_sha256,
            content_bytes=entry.content_bytes,
            content=content,
        )

    def _metadata(self) -> tuple[RunbookMetadata, ...]:
        records = tuple(self._load_metadata(name) for name in self._metadata_files)
        identities = {(item.runbook_id, item.version) for item in records}
        content_files = {item.content_file for item in records}
        if len(identities) != len(records) or len(content_files) != len(records):
            raise RunbookRegistryError("runbook_catalog_corrupt", "调查手册身份或正文路径重复")
        for item in records:
            self._content(item)
        return records

    def _load_metadata(self, filename: str) -> RunbookMetadata:
        if not filename or Path(filename).name != filename or not filename.endswith(".json"):
            raise RunbookRegistryError("runbook_catalog_corrupt", "调查手册元数据文件名无效")
        try:
            payload = (self._root / filename).read_bytes()
        except OSError:
            raise RunbookRegistryError("runbook_catalog_corrupt", "调查手册元数据缺失") from None
        if not 0 < len(payload) <= MAX_METADATA_BYTES or payload.startswith(b"\xef\xbb\xbf"):
            raise RunbookRegistryError("runbook_catalog_corrupt", "调查手册元数据大小或编码无效")
        try:
            text = payload.decode("utf-8")
            if "\r" in text or not text.endswith("\n"):
                raise ValueError
            raw = json.loads(text, object_pairs_hook=self._unique_json_object)
            if not isinstance(raw, dict):
                raise ValueError
            return RunbookMetadata.model_validate(raw)
        except (UnicodeDecodeError, ValueError, ValidationError):
            raise RunbookRegistryError(
                "runbook_catalog_corrupt", "调查手册元数据无法校验"
            ) from None

    @staticmethod
    def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    def _content(self, metadata: RunbookMetadata) -> str:
        try:
            payload = (self._root / metadata.content_file).read_bytes()
        except OSError:
            raise RunbookRegistryError("runbook_content_corrupt", "调查手册正文缺失") from None
        if (
            not 0 < len(payload) <= MAX_RUNBOOK_BYTES
            or payload.startswith(b"\xef\xbb\xbf")
            or b"\r" in payload
            or b"\x00" in payload
            or not payload.endswith(b"\n")
            or len(payload) != metadata.content_bytes
            or hashlib.sha256(payload).hexdigest() != metadata.content_sha256
        ):
            raise RunbookRegistryError("runbook_content_corrupt", "调查手册正文长度或散列无效")
        try:
            content = payload.decode("utf-8")
        except UnicodeDecodeError:
            raise RunbookRegistryError(
                "runbook_content_corrupt", "调查手册正文不是 UTF-8"
            ) from None
        if any(content.count(f"## {section}\n") != 1 for section in REQUIRED_SECTIONS):
            raise RunbookRegistryError("runbook_content_corrupt", "调查手册缺少固定内容边界")
        return content

    @staticmethod
    def _entry(metadata: RunbookMetadata) -> RunbookCatalogEntry:
        return RunbookCatalogEntry(
            material_id=runbook_material_id(
                metadata.runbook_id, metadata.version, metadata.content_sha256
            ),
            runbook_id=metadata.runbook_id,
            version=metadata.version,
            title=metadata.title,
            summary=metadata.summary,
            content_sha256=metadata.content_sha256,
            content_bytes=metadata.content_bytes,
        )

    @staticmethod
    def _matches(
        metadata: RunbookMetadata,
        request: InvestigationRequest,
        snapshot: TaskEvidenceSnapshot,
    ) -> bool:
        if (
            metadata.selector_version != "order-service-recent-completed-deployment-v1"
            or request.service_name != metadata.service_name
            or snapshot.service_name != metadata.service_name
            or snapshot.role is not metadata.role
        ):
            return False
        window_start = request.started_at - timedelta(hours=1)
        return any(
            evidence.kind is EvidenceKind.CHANGE
            and evidence.attributes.get("change_type") == "deployment"
            and evidence.attributes.get("completed") is True
            and window_start <= evidence.observed_at <= request.started_at
            for evidence in snapshot.evidence
        )

    @staticmethod
    def _catalog_hash(entries: tuple[RunbookCatalogEntry, ...]) -> str:
        payload = json.dumps(
            [entry.model_dump(mode="json") for entry in entries],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()
