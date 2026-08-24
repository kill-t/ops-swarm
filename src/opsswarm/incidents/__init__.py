"""Incident 领域模块的稳定公开接口。"""

from opsswarm.incidents.alerts import AlertIngestionResult, AlertSubmission
from opsswarm.incidents.errors import (
    IncidentDomainError,
    IncidentNotFound,
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
from opsswarm.incidents.state_machine import (
    create_incident,
    record_alert_attached,
    transition_incident,
)

__all__ = [
    "ActorType",
    "AlertIngestionResult",
    "AlertSubmission",
    "Incident",
    "IncidentAction",
    "IncidentActor",
    "IncidentDomainError",
    "IncidentEvent",
    "IncidentEventType",
    "IncidentMutation",
    "IncidentNotFound",
    "IncidentSeverity",
    "IncidentStatus",
    "IncidentValidationError",
    "IncidentVersionConflict",
    "InvalidIncidentTransition",
    "create_incident",
    "record_alert_attached",
    "transition_incident",
]
