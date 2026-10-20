import hashlib
import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

import opsswarm.investigation.runbooks as runbooks_package
from opsswarm.investigation.models import (
    EvidenceKind,
    EvidenceObservation,
    InvestigationRequest,
)
from opsswarm.investigation.runbooks import (
    ReadRunbookArguments,
    RunbookReadDetail,
    RunbookReader,
    RunbookRegistry,
    RunbookRegistryError,
    load_runbook,
    select_runbooks,
)
from opsswarm.investigation.runbooks.contracts import (
    MAX_RUNBOOK_BYTES,
    catalog_material_id,
    runbook_material_id,
)
from opsswarm.investigation.runbooks.registry import PUBLISHED_METADATA_FILES
from opsswarm.investigation.tasks.evidence import (
    TaskEvidenceRecord,
    TaskEvidenceSnapshot,
    evidence_hash,
)

STARTED_AT = datetime(2026, 1, 1, 8, 5, tzinfo=UTC)


def request(
    *, service_name: str = "order-service", title: str = "接口异常"
) -> InvestigationRequest:
    return InvestigationRequest(
        incident_id=uuid4(),
        service_name=service_name,
        title=title,
        started_at=STARTED_AT,
    )


def change_evidence(
    *,
    observed_at: datetime = STARTED_AT - timedelta(minutes=1),
    change_type: str = "deployment",
    completed: bool = True,
) -> EvidenceObservation:
    return EvidenceObservation(
        evidence_id="change-order-release",
        kind=EvidenceKind.CHANGE,
        source="frozen-change-store",
        summary="order-service 完成一次变更。",
        observed_at=observed_at,
        attributes={"change_type": change_type, "completed": completed},
    )


def evidence_record(
    evidence: EvidenceObservation | None = None,
    *,
    role: EvidenceKind = EvidenceKind.CHANGE,
    service_name: str = "order-service",
) -> TaskEvidenceRecord:
    selected = evidence or change_evidence()
    snapshot = TaskEvidenceSnapshot(
        evidence_snapshot_id="frozen-v2:test",
        role=role,
        service_name=service_name,
        window_start=STARTED_AT - timedelta(hours=2),
        window_end=STARTED_AT + timedelta(minutes=1),
        evidence=(selected,),
    )
    return TaskEvidenceRecord(
        task_id=uuid4(),
        investigation_run_id=uuid4(),
        attempt_id=uuid4(),
        call_id=uuid4(),
        snapshot=snapshot,
        content_sha256=evidence_hash(snapshot),
        created_at=STARTED_AT,
    )


def arguments_for(reader: RunbookReader) -> ReadRunbookArguments:
    entry = reader.catalog.entries[0]
    return ReadRunbookArguments(
        runbook_id=entry.runbook_id,
        version=entry.version,
        content_sha256=entry.content_sha256,
    )


def copied_registry(tmp_path: Path) -> RunbookRegistry:
    source = Path(runbooks_package.__file__).parent / "content"
    for filename in (*PUBLISHED_METADATA_FILES, "order-service-release-correlation-v1.md"):
        shutil.copyfile(source / filename, tmp_path / filename)
    return RunbookRegistry(root=tmp_path)


def test_recent_completed_deployment_selects_bounded_catalog_and_detail() -> None:
    record = evidence_record()
    catalog = select_runbooks(request(title="任意标题，不参与匹配"), record)

    assert len(catalog.entries) == 1
    entry = catalog.entries[0]
    assert entry.runbook_id == "order-service-release-correlation"
    assert entry.material_id == runbook_material_id(
        entry.runbook_id, entry.version, entry.content_sha256
    )
    assert catalog.material_id == catalog_material_id(catalog.version, catalog.content_sha256)

    detail = load_runbook(
        ReadRunbookArguments(
            runbook_id=entry.runbook_id,
            version=entry.version,
            content_sha256=entry.content_sha256,
        ),
        catalog=catalog,
    )
    assert detail.material_id == entry.material_id
    assert detail.authority == "guidance_only"
    assert detail.content_bytes == len(detail.content.encode("utf-8")) <= MAX_RUNBOOK_BYTES
    assert hashlib.sha256(detail.content.encode("utf-8")).hexdigest() == detail.content_sha256
    assert "手册不是事故证据" in detail.content
    bound = RunbookReadDetail(
        investigation_run_id=record.investigation_run_id,
        call_id=uuid4(),
        task_id=record.task_id,
        document=detail,
    )
    assert bound.document.material_id == entry.material_id


def test_material_identities_are_stable_across_registry_instances() -> None:
    current_request = request()
    record = evidence_record()

    first = select_runbooks(current_request, record, registry=RunbookRegistry())
    second = select_runbooks(current_request, record, registry=RunbookRegistry())

    assert first == second


@pytest.mark.parametrize(
    ("current_request", "record"),
    (
        (request(service_name="payment-service"), evidence_record()),
        (request(), evidence_record(service_name="payment-service")),
        (
            request(),
            evidence_record(
                EvidenceObservation(
                    evidence_id="log-order-error",
                    kind=EvidenceKind.LOG,
                    source="frozen-log-store",
                    summary="order-service 返回错误。",
                    observed_at=STARTED_AT - timedelta(minutes=1),
                    attributes={},
                ),
                role=EvidenceKind.LOG,
            ),
        ),
        (request(), evidence_record(change_evidence(change_type="configuration"))),
        (request(), evidence_record(change_evidence(completed=False))),
        (
            request(),
            evidence_record(
                change_evidence(observed_at=STARTED_AT - timedelta(hours=1, seconds=1))
            ),
        ),
        (
            request(),
            evidence_record(change_evidence(observed_at=STARTED_AT + timedelta(seconds=1))),
        ),
    ),
)
def test_selector_fails_closed_outside_stable_task_scope(
    current_request: InvestigationRequest,
    record: TaskEvidenceRecord,
) -> None:
    assert not select_runbooks(current_request, record).entries


def test_selector_includes_exact_one_hour_boundary_and_ignores_title() -> None:
    record = evidence_record(change_evidence(observed_at=STARTED_AT - timedelta(hours=1)))

    assert select_runbooks(request(title="Redis 故障且没有发布字样"), record).entries


def test_read_arguments_reject_path_and_whitespace_smuggling() -> None:
    with pytest.raises(ValidationError):
        ReadRunbookArguments.model_validate(
            {
                "runbook_id": "order-service-release-correlation",
                "version": "v1",
                "content_sha256": "0" * 64,
                "path": "../../secret",
            }
        )
    with pytest.raises(ValidationError):
        ReadRunbookArguments(
            runbook_id=" order-service-release-correlation",
            version="v1",
            content_sha256="0" * 64,
        )


@pytest.mark.parametrize(
    ("changes", "error_code"),
    (
        ({"runbook_id": "unknown-runbook"}, "runbook_not_found"),
        ({"version": "v2"}, "runbook_version_conflict"),
        ({"content_sha256": "0" * 64}, "runbook_version_conflict"),
    ),
)
def test_reader_rejects_unknown_or_conflicting_reference(
    changes: dict[str, str], error_code: str
) -> None:
    reader = RunbookReader(request=request(), task_evidence_record=evidence_record())
    arguments = arguments_for(reader).model_copy(update=changes)

    with pytest.raises(RunbookRegistryError) as captured:
        reader.load_runbook(arguments)

    assert captured.value.code == error_code


def test_reader_cannot_load_globally_published_book_outside_selected_scope() -> None:
    reader = RunbookReader(
        request=request(),
        task_evidence_record=evidence_record(change_evidence(change_type="configuration")),
    )
    metadata_hash = json.loads(
        (
            Path(runbooks_package.__file__).parent
            / "content"
            / "order-service-release-correlation-v1.json"
        ).read_text(encoding="utf-8")
    )["content_sha256"]

    with pytest.raises(RunbookRegistryError) as captured:
        reader.read(
            ReadRunbookArguments(
                runbook_id="order-service-release-correlation",
                version="v1",
                content_sha256=metadata_hash,
            )
        )

    assert captured.value.code == "runbook_not_found"


@pytest.mark.parametrize("mutation", ["content", "crlf", "oversized"])
def test_registry_rejects_tampered_or_unbounded_markdown(tmp_path: Path, mutation: str) -> None:
    registry = copied_registry(tmp_path)
    reader = RunbookReader(
        request=request(),
        task_evidence_record=evidence_record(),
        registry=registry,
    )
    markdown = tmp_path / "order-service-release-correlation-v1.md"
    if mutation == "content":
        markdown.write_bytes(markdown.read_bytes() + "篡改\n".encode())
    elif mutation == "crlf":
        markdown.write_bytes(markdown.read_bytes().replace(b"\n", b"\r\n"))
    else:
        markdown.write_bytes(b"# x\n" + b"a" * MAX_RUNBOOK_BYTES)

    with pytest.raises(RunbookRegistryError) as captured:
        reader.read(arguments_for(reader))

    assert captured.value.code == "runbook_content_corrupt"


def test_registry_rejects_unknown_metadata_field(tmp_path: Path) -> None:
    registry = copied_registry(tmp_path)
    metadata_file = tmp_path / PUBLISHED_METADATA_FILES[0]
    metadata = json.loads(metadata_file.read_text(encoding="utf-8"))
    metadata["unexpected"] = "not allowed"
    metadata_file.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )

    with pytest.raises(RunbookRegistryError) as captured:
        registry.catalog(request(), evidence_record().snapshot)

    assert captured.value.code == "runbook_catalog_corrupt"
