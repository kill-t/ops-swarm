"""Incident 领域模块的稳定公开接口。"""

from opsswarm.incidents.errors import (
    IncidentDomainError,
    IncidentValidationError,
    IncidentVersionConflict,
    InvalidIncidentTransition,
)
from opsswarm.incidents.models import (
    ActorType,
    Incident,
    IncidentAction,
    IncidentActor,
    IncidentEvent,
    IncidentEventType,
    IncidentMutation,
    IncidentSeverity,
    IncidentStatus,
)
from opsswarm.incidents.state_machine import create_incident, transition_incident

__all__ = [
    "ActorType",
    "Incident",
    "IncidentAction",
    "IncidentActor",
    "IncidentDomainError",
    "IncidentEvent",
    "IncidentEventType",
    "IncidentMutation",
    "IncidentSeverity",
    "IncidentStatus",
    "IncidentValidationError",
    "IncidentVersionConflict",
    "InvalidIncidentTransition",
    "create_incident",
    "transition_incident",
]
