"""将已校验的有界证据快照投影为目录，供首次输入和历史查询共用。"""

from opsswarm.investigation.context.evidence_contracts import (
    EvidenceCatalogEntry,
    EvidenceCatalogGroup,
)
from opsswarm.investigation.tasks.evidence import TaskEvidenceRecord


def catalog_group(record: TaskEvidenceRecord) -> EvidenceCatalogGroup:
    snapshot = record.snapshot
    return EvidenceCatalogGroup(
        task_id=record.task_id,
        version=snapshot.version,
        snapshot_id=snapshot.evidence_snapshot_id,
        snapshot_sha256=record.content_sha256,
        data_source_mode=snapshot.data_source_mode,
        service_name=snapshot.service_name,
        role=snapshot.role,
        window_start=snapshot.window_start,
        window_end=snapshot.window_end,
        entries=tuple(
            EvidenceCatalogEntry(
                evidence_id=e.evidence_id,
                kind=e.kind,
                source=e.source,
                summary=e.summary,
                observed_at=e.observed_at,
            )
            for e in sorted(snapshot.evidence, key=lambda e: e.evidence_id)
        ),
    )
