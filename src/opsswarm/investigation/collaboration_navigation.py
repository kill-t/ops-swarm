"""通过一次只读查询定位最新现场调查及其业务工件。"""

from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opsswarm.investigation.context.hashing import content_hash
from opsswarm.investigation.handoff.contracts import TerminalHandoffRecord
from opsswarm.investigation.human_routing.contracts import HumanRoutingRecord
from opsswarm.investigation.workflow_versions import LIVE_DYNAMIC_INVESTIGATION_GRAPH_VERSION

_NAVIGATION = text("""
WITH latest AS (
  SELECT id, status, graph_version FROM investigation_runs
  WHERE incident_id=:incident ORDER BY created_at DESC, id DESC LIMIT 1
), current_run AS (
  SELECT latest.*,
    (SELECT max(state_version) FROM investigation_human_wait_events WHERE
      investigation_run_id=latest.id) AS wait_version
  FROM latest WHERE graph_version=:graph
)
SELECT r.id AS run_id, r.status, to_jsonb(h) AS handoff,
  p.id AS preparation_id, q.id AS review_job_id, to_jsonb(route) AS human_route,
  (SELECT id FROM investigation_candidate_evaluation_jobs c
   WHERE c.investigation_run_id=r.id AND c.wait_version IS NOT DISTINCT FROM r.wait_version
   ORDER BY c.created_at DESC, c.id DESC LIMIT 1) AS candidate_id,
  (SELECT review->>'rationale' FROM investigation_hypothesis_reviews
   WHERE investigation_run_id=r.id ORDER BY created_at DESC, id DESC LIMIT 1) AS review_rationale,
  (SELECT ARRAY(SELECT item#>>'{entry,id}' FROM jsonb_array_elements(x.content->'items') item)
   FROM investigation_experience_contexts x WHERE x.investigation_run_id=r.id) AS experience_ids
FROM current_run r
LEFT JOIN investigation_terminal_handoffs h ON h.investigation_run_id=r.id
LEFT JOIN investigation_human_preparation_jobs p
  ON p.investigation_run_id=r.id AND p.wait_version=r.wait_version
LEFT JOIN investigation_human_review_jobs q ON q.preparation_job_id=p.id
LEFT JOIN investigation_human_review_routes route ON route.job_id=q.id
""")


class CollaborationNavigation(BaseModel):
    run_id: UUID
    status: str
    handoff: TerminalHandoffRecord | None
    handoff_sha256: str | None
    preparation_id: UUID | None
    review_job_id: UUID | None
    review_route_sha256: str | None
    candidate_id: UUID | None
    review_rationale: str | None
    experience_ids: tuple[UUID, ...] | None


async def read_collaboration_navigation(
    factory: async_sessionmaker[AsyncSession], incident_id: UUID
) -> CollaborationNavigation | None:
    async with factory() as session:
        result = await session.execute(
            _NAVIGATION,
            {"incident": incident_id, "graph": LIVE_DYNAMIC_INVESTIGATION_GRAPH_VERSION},
        )
        row = result.mappings().one_or_none()
    if row is None:
        return None
    facts = dict(row)
    handoff_data = facts.pop("handoff")
    route_data = facts.pop("human_route")
    handoff = TerminalHandoffRecord.model_validate(handoff_data) if handoff_data else None
    route = HumanRoutingRecord.model_validate(route_data) if route_data else None
    return CollaborationNavigation(
        **facts,
        handoff=handoff,
        handoff_sha256=content_hash(handoff) if handoff else None,
        review_route_sha256=content_hash(route) if route else None,
    )
