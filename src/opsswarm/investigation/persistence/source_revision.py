"""用有界物理行标记复核来源集合的新鲜度，不加载大正文。"""

import json
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from opsswarm.investigation.tasks.errors import TaskFactConflict

# 表、字段与条件均由代码固定，不接受模型或调用方提供的 SQL 标识符。
_RUN_TABLES = (
    ("work_items", "id"),
    ("investigation_attempts", "id"),
    ("investigation_rounds", "id"),
    ("agent_tasks", "id"),
    ("agent_task_attempts", "id"),
    ("agent_task_evidence", "task_id"),
    ("agent_task_results", "task_id"),
    ("agent_task_working_summaries", "task_id"),
    ("investigation_call_reservations", "id"),
    ("investigation_coordination_policies", "investigation_run_id"),
    ("investigation_coordination_plans", "round_id"),
    ("investigation_open_hypotheses", "id"),
    ("investigation_hypothesis_reviews", "id"),
    ("investigation_hypothesis_review_routes", "id"),
    ("investigation_experience_contexts", "investigation_run_id"),
)


def _marked(table: str, key: str, condition: str) -> str:
    return f"""
      SELECT '{table}' AS name,
        COALESCE(jsonb_agg(jsonb_build_array(identity, revision, location)
                          ORDER BY identity), '[]'::jsonb) AS marks
      FROM (SELECT t.{key}::text AS identity, t.xmin::text AS revision,
                   t.ctid::text AS location
            FROM {table} t WHERE {condition} ORDER BY t.{key} LIMIT 513) bounded
    """


_MARKS = [_marked(table, key, "t.investigation_run_id = :run_id") for table, key in _RUN_TABLES] + [
    _marked("investigation_runs", "id", "t.id = :run_id"),
    _marked("incidents", "id", "t.id IN (SELECT incident_id FROM current_run)"),
    _marked("experience_entries", "id", "t.id IN (SELECT id FROM chosen_entries)"),
    _marked("experience_reviews", "id", "t.id IN (SELECT review_id FROM chosen_entries)"),
    _marked("experience_candidates", "id", "t.id IN (SELECT candidate_id FROM chosen_entries)"),
    _marked("experience_generation_jobs", "id", "t.id IN (SELECT job_id FROM chosen_candidates)"),
    _marked(
        "operator_authorizations", "id", "t.id IN (SELECT authorization_id FROM chosen_reviews)"
    ),
]
_QUERY = text(
    """
    WITH current_run AS (SELECT incident_id FROM investigation_runs WHERE id=:run_id),
    chosen_entries AS (
      SELECT e.id, e.review_id, e.candidate_id
      FROM investigation_experience_contexts c
      CROSS JOIN LATERAL jsonb_array_elements(
        CASE WHEN jsonb_typeof(c.content->'items')='array' THEN c.content->'items'
             ELSE '[]'::jsonb END) item
      JOIN experience_entries e ON e.id::text = item #>> '{entry,id}'
      WHERE c.investigation_run_id=:run_id
    ),
    chosen_candidates AS (
      SELECT job_id FROM experience_candidates WHERE id IN (SELECT candidate_id FROM chosen_entries)
    ),
    chosen_reviews AS (
      SELECT authorization_id FROM experience_reviews
      WHERE id IN (SELECT review_id FROM chosen_entries)
    )
    SELECT jsonb_agg(jsonb_build_array(name, marks) ORDER BY name) FROM (
    """
    + " UNION ALL ".join(_MARKS)
    + ") dependencies"
)


async def read_source_stamp(session: AsyncSession, run_id: UUID) -> str:
    marks = await session.scalar(_QUERY, {"run_id": run_id})
    if (
        not isinstance(marks, list)
        or any(len(group[1]) > 512 for group in marks)
        or sum(len(group[1]) for group in marks) > 4096
    ):
        raise TaskFactConflict("source_validation_limit", "来源依赖集合超过核验上限")
    # 比较完整标记，不依赖散列碰撞假设；xmin/ctid 只在当前短作用域内有效。
    return json.dumps(marks, separators=(",", ":"))
