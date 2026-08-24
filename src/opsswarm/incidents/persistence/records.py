"""持久化层返回给应用层的只读记录。"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from opsswarm.incidents.errors import IncidentValidationError
from opsswarm.incidents.models import Incident, IncidentEvent


@dataclass(frozen=True, slots=True)
class StoredIncident:
    """领域快照及数据库分配的展示序号。"""

    incident: Incident
    display_number: int

    def __post_init__(self) -> None:
        if self.display_number < 1:
            raise IncidentValidationError("display_number 必须大于等于 1")


@dataclass(frozen=True, slots=True)
class StoredIncidentEvent:
    """带全局递增游标和附加数据的时间线事件。"""

    cursor: int
    event: IncidentEvent
    payload: Mapping[str, Any]

    def __post_init__(self) -> None:
        if self.cursor < 1:
            raise IncidentValidationError("事件 cursor 必须大于等于 1")
        if not isinstance(self.payload, Mapping):
            raise IncidentValidationError("事件 payload 必须是对象")
        object.__setattr__(self, "payload", dict(self.payload))
