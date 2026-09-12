"""按已保存调用绑定证据范围，提供目录和原始详情的只读用例。"""

from uuid import UUID

from opsswarm.investigation.budget.queries import CallBudgetQueries
from opsswarm.investigation.context.catalog import catalog_group
from opsswarm.investigation.context.contracts import (
    ContextManifest,
    ContextMaterial,
)
from opsswarm.investigation.context.evidence_contracts import (
    ContextEvidenceDetail,
    EvidenceCatalog,
    EvidenceReadRequest,
)
from opsswarm.investigation.models import EvidenceKind
from opsswarm.investigation.tasks.errors import TaskFactConflict
from opsswarm.investigation.tasks.evidence import TaskEvidenceRecord
from opsswarm.investigation.tasks.queries import CollaborationQueries


class ContextEvidenceReader:
    """调用范围构造后固定；读取参数不能扩张到其他调用或运行。"""

    def __init__(
        self,
        *,
        run_id: UUID,
        call_id: UUID,
        calls: CallBudgetQueries,
        tasks: CollaborationQueries,
    ) -> None:
        self._run_id, self._call_id = run_id, call_id
        self._calls, self._tasks = calls, tasks

    async def _manifest(self) -> ContextManifest | None:
        try:
            manifest = await self._calls.get_context_manifest(self._run_id, self._call_id)
        except ValueError:
            raise TaskFactConflict("context_evidence_corrupt", "调用清单无法校验") from None
        if manifest is None:
            return None
        refs = self._references(manifest)
        ids = {r.material_id for r in refs}
        if (
            manifest.investigation_run_id != self._run_id
            or len(ids) != len(refs)
            or (manifest.role == "synthesis" and (manifest.task_id is not None or len(refs) != 3))
            or (manifest.role != "synthesis" and (len(refs) != 1 or ids != {manifest.task_id}))
        ):
            raise TaskFactConflict("context_evidence_corrupt", "调用清单的专业范围不一致")
        return manifest

    @staticmethod
    def _references(manifest: ContextManifest) -> tuple[ContextMaterial, ...]:
        return tuple(m for m in manifest.materials if m.kind == "task_evidence")

    async def _record(self, manifest: ContextManifest, ref: ContextMaterial) -> TaskEvidenceRecord:
        try:
            record = await self._tasks.get_evidence(self._run_id, ref.material_id)
        except (TaskFactConflict, ValueError):
            raise TaskFactConflict("context_evidence_corrupt", "清单引用的材料无法校验") from None
        if record is None:
            raise TaskFactConflict("context_evidence_corrupt", "清单引用的材料已缺失")
        snapshot = record.snapshot
        if (
            record.investigation_run_id != self._run_id
            or record.task_id != ref.material_id
            or record.content_sha256 != ref.content_sha256
            or snapshot.version != ref.version
            or snapshot.version != manifest.evidence_policy_version
            or tuple(e.evidence_id for e in snapshot.evidence) != ref.evidence_ids
            or (manifest.role != "synthesis" and snapshot.role != manifest.role)
        ):
            raise TaskFactConflict("context_evidence_corrupt", "材料与调用时记录的版本或范围不一致")
        return record

    async def catalog(self) -> EvidenceCatalog | None:
        manifest = await self._manifest()
        if manifest is None or manifest.version == "coordination-context-v1":
            return None
        groups = []
        for ref in self._references(manifest):
            record = await self._record(manifest, ref)
            groups.append(catalog_group(record))
        if manifest.role == "synthesis" and {g.role for g in groups} != set(EvidenceKind):
            raise TaskFactConflict("context_evidence_corrupt", "综合清单没有完整的专业范围")
        return EvidenceCatalog(
            investigation_run_id=self._run_id,
            call_id=self._call_id,
            context_policy_version=manifest.version,
            groups=tuple(sorted(groups, key=lambda g: (g.role.value, str(g.task_id)))),
        )

    async def read(self, request: EvidenceReadRequest) -> ContextEvidenceDetail:
        manifest = await self._manifest()
        if manifest is None:
            raise TaskFactConflict("context_evidence_unavailable", "该调用没有可追溯的上下文材料")
        ref = next(
            (r for r in self._references(manifest) if r.material_id == request.task_id), None
        )
        if ref is None or request.evidence_id not in ref.evidence_ids:
            raise TaskFactConflict("context_evidence_not_found", "该调用未引用指定证据")
        if request.snapshot_sha256 != ref.content_sha256:
            raise TaskFactConflict(
                "context_evidence_version_conflict", "证据引用与调用快照版本不一致"
            )
        record = await self._record(manifest, ref)
        evidence = next(e for e in record.snapshot.evidence if e.evidence_id == request.evidence_id)
        return ContextEvidenceDetail(
            investigation_run_id=self._run_id,
            call_id=self._call_id,
            task_id=request.task_id,
            version=record.snapshot.version,
            snapshot_id=record.snapshot.evidence_snapshot_id,
            snapshot_sha256=record.content_sha256,
            evidence=evidence,
        )
