"""在预算事务冻结首轮协调经验，并按不可变工件恢复历史选择。"""

import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid5

from sqlalchemy.ext.asyncio import AsyncSession

from opsswarm.experience.artifact_validation import validated_source
from opsswarm.experience.context_contracts import ExperienceContextItem, ExperienceContextSelection
from opsswarm.experience.context_tables import ExperienceContextRow
from opsswarm.experience.contracts import ExperienceError, canonical_hash
from opsswarm.experience.lifecycle_contracts import ExperienceSearchQuery, SearchScope
from opsswarm.experience.review_store import load_confirmed_entry_in_session
from opsswarm.experience.search_store import search_in_session
from opsswarm.experience.store import utc_now
from opsswarm.experience.tables import ExperienceCandidateRow, ExperienceJobRow
from opsswarm.incidents.persistence.tables import IncidentRow
from opsswarm.investigation.budget.tables import CallReservationRow
from opsswarm.investigation.models import InvestigationDataSourceMode
from opsswarm.investigation.persistence.tables import InvestigationRunRow
from opsswarm.investigation.tasks.errors import TaskFactConflict

_POLICY = "coordination-experience-v1"
_INPUT_VERSION = "coordination-experience-input-v1"


async def prepare_experience_selection(
    session: AsyncSession,
    run: InvestigationRunRow,
    *,
    clock: Callable[[], datetime] = utc_now,
) -> ExperienceContextSelection:
    """已有冻结记录只走历史复核；新选择仍是未写库的有界提案。"""
    try:
        _require_enabled(run)
        existing = await load_experience_selection(session, run)
        if existing is not None:
            return existing
        selected_at = _utc(clock())
        if selected_at < _utc(run.created_at):
            raise _conflict("time", "经验选择时间早于运行创建时间")
        return await _current_selection(session, run, selected_at)
    except (ExperienceError, ValueError, TypeError):
        raise _conflict("source", "协调经验准备材料无法校验") from None


async def validate_experience_selection(
    session: AsyncSession,
    run: InvestigationRunRow,
    selection: ExperienceContextSelection,
    *,
    first_call_id: UUID | None = None,
    reserved_at: datetime | None = None,
) -> ExperienceContextSelection:
    """首次预留重查有效集合并冻结；任何外层失败由调用方事务整体回滚。"""
    try:
        _require_enabled(run)
        existing = await load_experience_selection(session, run)
        if existing is not None:
            if not _same(existing, selection):
                raise _conflict("selection", "经验选择与已冻结的运行材料不一致")
            return existing
        if first_call_id is None or reserved_at is None:
            raise _conflict("not_frozen", "新经验选择必须绑定首次模型预留")
        freeze_at = _utc(reserved_at)
        selected_at = _utc(selection.selected_at)
        if not _utc(run.created_at) <= selected_at <= freeze_at:
            raise _conflict("time", "经验提案时间不在运行创建与预算预留之间")
        expected = await _current_selection(session, run, selected_at)
        if not _same(expected, selection):
            raise _conflict("selection", "经验准备后有效集合、顺序或工件发生变化")
        row = ExperienceContextRow(
            investigation_run_id=run.id,
            id=expected.id,
            policy_version=_POLICY,
            first_call_id=first_call_id,
            content=expected.model_dump(mode="json"),
            content_hash=canonical_hash(expected),
            created_at=freeze_at,
        )
        session.add(row)
        # 首次调用行由同一预算事务稍后插入，复合延迟外键在提交时闭合。
        await session.flush()
        return expected
    except (ExperienceError, ValueError, TypeError):
        raise _conflict("source", "协调经验预留材料无法校验") from None


async def load_experience_selection(
    session: AsyncSession, run: InvestigationRunRow
) -> ExperienceContextSelection | None:
    """恢复冻结选择，不读取当前停用、替代或重新检索的有效集合。"""
    try:
        row = await session.get(ExperienceContextRow, run.id)
        if row is None:
            return None
        _require_enabled(run)
        selection = ExperienceContextSelection.model_validate(row.content)
        scope = await _scope(session, run)
        if (
            row.investigation_run_id != run.id
            or row.id != _selection_id(run.id)
            or row.id != selection.id
            or row.policy_version != _POLICY
            or row.policy_version != selection.policy_version
            or selection.scope != scope
            or row.content != selection.model_dump(mode="json")
            or row.content_hash != canonical_hash(selection)
            or not _utc(run.created_at) <= _utc(selection.selected_at) <= _utc(row.created_at)
        ):
            raise _conflict("integrity", "冻结经验选择的身份、范围、时间或散列不一致")
        expected_items = tuple([await _item(session, item.entry.id) for item in selection.items])
        if selection.items != expected_items:
            raise _conflict("integrity", "冻结经验选择与不可变确认工件不一致")
        first_call = await session.get(CallReservationRow, row.first_call_id)
        _validate_first_call(first_call, run, selection, _utc(row.created_at))
        return selection
    except (ExperienceError, ValueError, TypeError):
        raise _conflict("source", "历史协调经验材料无法校验") from None


def _selection_id(run_id: UUID) -> UUID:
    return uuid5(run_id, _POLICY)


def _require_enabled(run: InvestigationRunRow) -> None:
    if (
        getattr(run, "coordination_experience_policy_version", None) != _POLICY
        or run.workflow_version != "v3"
        or run.graph_version != "dynamic-investigation-graph-v15"
        or run.data_source_mode != "frozen"
        or run.mode != "multi_agent"
    ):
        raise _conflict("policy", "运行未固定受支持的首轮协调经验策略")


async def _scope(session: AsyncSession, run: InvestigationRunRow) -> SearchScope:
    incident = await session.get(IncidentRow, run.incident_id)
    if incident is None:
        raise _conflict("scope", "协调经验运行缺少持久事故范围")
    return SearchScope(
        run_id=run.id,
        incident_id=incident.id,
        service_name=incident.service_name,
        data_source_mode=InvestigationDataSourceMode(run.data_source_mode),
    )


async def _current_selection(
    session: AsyncSession, run: InvestigationRunRow, selected_at: datetime
) -> ExperienceContextSelection:
    result = await search_in_session(
        session, run.id, ExperienceSearchQuery(), clock=lambda: selected_at
    )
    if (
        result.scope != await _scope(session, run)
        or result.policy_version != "experience-search-v2"
    ):
        raise _conflict("scope", "协调经验检索没有使用运行固定范围")
    items = []
    for summary in result.items:
        item = await _item(session, summary.entry_id)
        if (
            item.source_incident_id != summary.source_incident_id
            or item.entry.version != summary.entry_version
            or item.entry.content_hash != summary.content_hash
            or item.entry.source_hash != summary.source_hash
        ):
            raise _conflict("integrity", "协调经验检索摘要与完整工件不一致")
        items.append(item)
    return ExperienceContextSelection(
        id=_selection_id(run.id),
        scope=result.scope,
        selected_at=selected_at,
        items=tuple(items),
    )


async def _item(session: AsyncSession, entry_id: UUID) -> ExperienceContextItem:
    entry = await load_confirmed_entry_in_session(session, entry_id)
    candidate = await session.get(ExperienceCandidateRow, entry.candidate_id)
    if candidate is None:
        raise _conflict("integrity", "经验工件缺少冻结候选来源")
    job = await session.get(ExperienceJobRow, candidate.job_id)
    if job is None:
        raise _conflict("integrity", "经验候选缺少生成来源")
    source = validated_source(job)
    return ExperienceContextItem(source_incident_id=source.incident_id, entry=entry)


def _validate_first_call(
    call: CallReservationRow | None,
    run: InvestigationRunRow,
    selection: ExperienceContextSelection,
    reserved_at: datetime,
) -> None:
    if (
        call is None
        or call.investigation_run_id != run.id
        or call.kind != "model"
        or call.operation != "coordinate"
        or call.task_id is not None
        or call.task_attempt_id is not None
        or call.invocation_index != 0
        or call.status not in {"reserved", "succeeded", "failed", "unknown"}
        or _utc(call.reserved_at) != reserved_at
    ):
        raise _conflict("call", "经验首次冻结来源不是同运行的根级协调预留")
    model_input = call.model_input
    manifest = call.context_manifest
    if (
        not isinstance(model_input, dict)
        or model_input.get("version") != _INPUT_VERSION
        or not isinstance(manifest, dict)
        or manifest.get("version") != _POLICY
        or manifest.get("investigation_run_id") != str(run.id)
        or manifest.get("task_id") is not None
        or manifest.get("role") != "coordinator"
        or manifest.get("role_version") != "dynamic-coordinator-experience-v1"
        or manifest.get("prompt_version") != "coordination-experience-plan-v1"
        or manifest.get("workflow_version") != "v3"
        or manifest.get("tool_names") != []
    ):
        raise _conflict("call", "经验首次调用的输入或清单策略不一致")
    messages = model_input.get("messages")
    if (
        not isinstance(messages, list)
        or len(messages) != 2
        or not isinstance(messages[1], dict)
        or messages[1].get("role") != "user"
        or not isinstance(messages[1].get("content"), str)
    ):
        raise _conflict("call", "经验首次调用缺少完整用户材料")
    payload = json.loads(messages[1]["content"])
    if not isinstance(payload, dict) or payload.get(
        "historical_experience"
    ) != selection.model_dump(mode="json"):
        raise _conflict("call", "经验冻结选择没有完整进入首次模型输入")
    materials = manifest.get("materials")
    if not isinstance(materials, list) or any(not isinstance(item, dict) for item in materials):
        raise _conflict("call", "经验首次调用材料清单不完整")
    expected_selection = [(str(selection.id), selection.policy_version, canonical_hash(selection))]
    expected_entries = [
        (str(item.entry.id), str(item.entry.version), item.entry.content_hash)
        for item in selection.items
    ]
    if (
        _material_refs(materials, "experience_selection") != expected_selection
        or _material_refs(materials, "confirmed_experience") != expected_entries
    ):
        raise _conflict("call", "经验首次调用没有绑定完整选择和确认版本引用")


def _material_refs(materials: list[Any], kind: str) -> list[tuple[Any, Any, Any]]:
    selected = [item for item in materials if item.get("kind") == kind]
    if any(item.get("evidence_ids", []) != [] for item in selected):
        raise _conflict("call", "历史经验材料不能声明当前事故证据引用")
    return [
        (item.get("material_id"), item.get("version"), item.get("content_sha256"))
        for item in selected
    ]


def _same(left: ExperienceContextSelection, right: ExperienceContextSelection) -> bool:
    return left == right and canonical_hash(left) == canonical_hash(right)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("协调经验时间必须包含时区")
    return value.astimezone(UTC)


def _conflict(kind: str, message: str) -> TaskFactConflict:
    return TaskFactConflict(f"experience_context_{kind}_conflict", message)
