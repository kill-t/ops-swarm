"""单次来源遍历的有界复用，动态依赖变化时整次失败。"""

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from functools import wraps
from typing import TypeVar, cast
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import event, inspect
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from opsswarm.investigation.persistence.source_revision import read_source_stamp
from opsswarm.investigation.tasks.errors import TaskFactConflict

_Row = TypeVar("_Row")
_Result = TypeVar("_Result", bound=BaseModel)
_SCOPE_KEY = "opsswarm_source_validation"


def _changed() -> TaskFactConflict:
    return TaskFactConflict("source_validation_changed", "来源在本次核验中变化，请重新读取")


def _loaded(row: object) -> dict[str, str]:
    state = inspect(row)
    assert state is not None
    return {
        attr.key: json.dumps(state.dict[attr.key], sort_keys=True, default=str, ensure_ascii=False)
        for attr in state.mapper.column_attrs
        if attr.key in state.dict
    }


class _ValidationScope:
    def __init__(self, session: AsyncSession, run_id: UUID) -> None:
        self.session = session
        self.run_id = run_id
        self.transaction = session.sync_session.get_transaction()
        self.nested = session.sync_session.get_nested_transaction()
        self.owner = asyncio.current_task()
        self.stamp = ""
        self.failed = False
        self.entries: dict[tuple[object, str], BaseModel] = {}
        self.visiting: set[tuple[object, str]] = set()
        self.loaded: dict[object, dict[str, str]] = {}

    def invalidate(self, *args: object) -> None:
        self.failed = True
        self.entries.clear()

    async def initialize(self) -> None:
        self.stamp = await read_source_stamp(self.session, self.run_id)
        # 已加载对象可能早于首戳；不把新数据库标记误绑定到旧 identity map。
        for row in list(self.session.identity_map.values()):
            previous = _loaded(row)
            if previous:
                await self.session.refresh(row, attribute_names=list(previous))
                if _loaded(row) != previous:
                    raise _changed()
        await self.check()

    async def check(self) -> None:
        try:
            await self._check()
        except BaseException:
            self.failed = True
            self.entries.clear()
            raise

    async def _check(self) -> None:
        session = self.session
        if (
            self.failed
            or self.transaction is not session.sync_session.get_transaction()
            or self.nested is not session.sync_session.get_nested_transaction()
            or self.owner is not asyncio.current_task()
            or session.new
            or session.dirty
            or session.deleted
        ):
            raise _changed()
        for row in list(session.identity_map.values()):
            current = _loaded(row)
            previous = self.loaded.get(row)
            if previous is not None and any(current.get(k) != v for k, v in previous.items()):
                raise _changed()
            self.loaded[row] = current
        if await read_source_stamp(session, self.run_id) != self.stamp:
            raise _changed()

    async def validate(
        self, function: Callable[[AsyncSession, _Row], Awaitable[_Result]], row: _Row
    ) -> _Result:
        await self.check()
        key = (
            function,
            json.dumps(_loaded(row), sort_keys=True),
        )
        if key in self.visiting:
            self.invalidate()
            raise TaskFactConflict("source_validation_cycle", "来源关系存在循环")
        if key in self.entries:
            # 返回副本，避免 frozen 模型中的嵌套字典污染其他路径。
            return cast(_Result, self.entries[key].model_copy(deep=True))
        if len(self.entries) + len(self.visiting) >= 64:
            self.invalidate()
            raise TaskFactConflict("source_validation_limit", "来源核验节点超过上限")
        self.visiting.add(key)
        try:
            result = await function(self.session, row)
            await self.check()
            self.entries[key] = result.model_copy(deep=True)
            return result
        except BaseException:
            self.failed = True
            self.entries.clear()
            raise
        finally:
            self.visiting.remove(key)


@asynccontextmanager
async def source_validation_scope(
    session: AsyncSession, run_id: UUID
) -> AsyncIterator[_ValidationScope | None]:
    """只组织一次完整只读遍历，不跨业务操作保留已核验材料。"""

    if not isinstance(getattr(session, "sync_session", None), Session):
        yield None
        return
    existing = session.info.get(_SCOPE_KEY)
    if isinstance(existing, _ValidationScope):
        if existing.run_id != run_id:
            existing.invalidate()
            raise _changed()
        await existing.check()
        try:
            yield existing
            await existing.check()
        except BaseException:
            existing.invalidate()
            raise
        return
    if session.new or session.dirty or session.deleted:
        yield None
        return
    from opsswarm.investigation.persistence.tables import InvestigationRunRow

    run = await session.get(InvestigationRunRow, run_id)
    if run is None or run.workflow_version != "v3":
        yield None
        return
    scope = _ValidationScope(session, run_id)
    session.info[_SCOPE_KEY] = scope
    callback = scope.invalidate
    events = ("after_transaction_create", "after_transaction_end", "before_flush")
    for name in events:
        event.listen(session.sync_session, name, callback)
    try:
        await scope.initialize()
        yield scope
        await scope.check()
    except BaseException:
        scope.invalidate()
        raise
    finally:
        for name in events:
            event.remove(session.sync_session, name, callback)
        session.info.pop(_SCOPE_KEY, None)


def reuse_validated_source[Row, Result: BaseModel](
    function: Callable[[AsyncSession, Row], Awaitable[Result]],
) -> Callable[[AsyncSession, Row], Awaitable[Result]]:
    @wraps(function)
    async def wrapped(session: AsyncSession, row: Row) -> Result:
        # 假仓储与旧 v2 路径沿用原函数；未落库行不成为可复用来源。
        if not isinstance(getattr(session, "sync_session", None), Session):
            return await function(session, row)
        run_id = getattr(row, "investigation_run_id", None)
        state = inspect(row, raiseerr=False)
        scope = session.info.get(_SCOPE_KEY)
        if isinstance(scope, _ValidationScope):
            if scope.run_id != run_id:
                scope.failed = True
                raise _changed()
            return await scope.validate(function, row)
        if (
            not isinstance(run_id, UUID)
            or state is None
            or not state.persistent
            or state.session is not session.sync_session
        ):
            return await function(session, row)
        async with source_validation_scope(session, run_id) as scope:
            if scope is None:
                return await function(session, row)
            return await scope.validate(function, row)

    return wrapped
