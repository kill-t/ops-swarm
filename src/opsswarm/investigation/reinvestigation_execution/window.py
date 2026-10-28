"""将分派范围向内收缩为既有只读端口允许的实际窗口。"""

from datetime import timedelta

from opsswarm.investigation.models import EvidenceKind
from opsswarm.investigation.reinvestigation_coordination.contracts import ReflowError
from opsswarm.investigation.reinvestigation_execution.contracts import EffectiveWindow
from opsswarm.investigation.tasks.contracts import TaskAssignment
from opsswarm.observation_window import ObservationWindow


def effective_window(assignment: TaskAssignment) -> EffectiveWindow:
    start = assignment.window_start.replace(microsecond=0)
    if assignment.window_start.microsecond:
        start += timedelta(seconds=1)
    end = assignment.window_end.replace(microsecond=0)
    adjustments = []
    if (start, end) != (assignment.window_start, assignment.window_end):
        adjustments.append("round_seconds")
    if assignment.role in (EvidenceKind.LOG, EvidenceKind.METRIC):
        minutes = int((end - start).total_seconds() // 60)
        if minutes < 1:
            raise ReflowError("execution_window_too_short", "新取证窗口不足一个完整分钟")
        aligned = end - timedelta(minutes=minutes)
        if aligned != start:
            adjustments.append("inward_minutes")
        start = aligned
    if start >= end:
        raise ReflowError("execution_window_too_short", "新取证窗口不足一个完整秒")
    return EffectiveWindow(
        requested_start=assignment.window_start,
        requested_end=assignment.window_end,
        window=ObservationWindow(window_start=start, window_end=end),
        adjustments=tuple(adjustments),
    )
