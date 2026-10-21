"""动态图终端路由的持久交接契约；交接不授予执行资格。"""

from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field

from opsswarm.investigation.models import StrictModel

TerminalDestination = Literal["await_human", "candidate_evaluation"]


class TerminalHandoffRecord(StrictModel):
    """来源路由与本次交接尝试分别保存，允许新租约复用旧路由。"""

    id: UUID
    investigation_run_id: UUID
    investigation_attempt_id: UUID
    source_review_route_id: UUID
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    version: Literal["terminal-handoff-v1"] = "terminal-handoff-v1"
    destination: TerminalDestination
    created_at: AwareDatetime
