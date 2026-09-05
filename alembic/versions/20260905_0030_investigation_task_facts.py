"""新增轮次、专业任务和尝试事实，保留原有调查运行数据。"""

from alembic import op

revision = "0030_investigation_task_facts"
down_revision = "0029_follow_up_governance_idx"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """创建有界任务事实与运行归属约束。"""
    op.create_unique_constraint(
        "uq_investigation_attempts_id_run",
        "investigation_attempts",
        ["id", "investigation_run_id"],
    )
    op.execute(
        """CREATE TABLE investigation_rounds (
    id UUID NOT NULL,
    investigation_run_id UUID NOT NULL,
    round_number INTEGER NOT NULL,
    goal VARCHAR(1000) NOT NULL,
    specification JSONB NOT NULL,
    content_hash VARCHAR(64) NOT NULL,
    status VARCHAR(16) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    completed_at TIMESTAMP WITH TIME ZONE,
    PRIMARY KEY (id),
    CONSTRAINT uq_rounds_run_number UNIQUE (investigation_run_id, round_number),
    CONSTRAINT uq_rounds_id_run UNIQUE (id, investigation_run_id),
    CONSTRAINT ck_rounds_number CHECK (round_number BETWEEN 1 AND 3),
    CONSTRAINT ck_rounds_status CHECK (status IN ('running', 'completed', 'failed')),
    CONSTRAINT ck_rounds_completion CHECK ((status = 'running') = (completed_at IS NULL)),
    CONSTRAINT ck_rounds_time CHECK (completed_at >= created_at),
    CONSTRAINT ck_rounds_specification CHECK (jsonb_typeof(specification) = 'object' AND
        octet_length(specification::text) <= 32768),
    FOREIGN KEY(investigation_run_id) REFERENCES investigation_runs (id) ON DELETE RESTRICT
)
        """
    )
    op.execute(
        """CREATE TABLE agent_tasks (
    id UUID NOT NULL,
    investigation_run_id UUID NOT NULL,
    round_id UUID NOT NULL,
    task_key VARCHAR(64) NOT NULL,
    assignment JSONB NOT NULL,
    status VARCHAR(16) NOT NULL,
    version INTEGER NOT NULL,
    attempt_count INTEGER NOT NULL,
    max_attempts INTEGER NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
    completed_at TIMESTAMP WITH TIME ZONE,
    PRIMARY KEY (id),
    CONSTRAINT fk_tasks_round_run FOREIGN KEY(round_id, investigation_run_id) REFERENCES
        investigation_rounds (id, investigation_run_id) ON DELETE RESTRICT,
    CONSTRAINT uq_tasks_round_key UNIQUE (round_id, task_key),
    CONSTRAINT uq_tasks_id_run UNIQUE (id, investigation_run_id),
    CONSTRAINT ck_tasks_status CHECK (status IN ('pending', 'running', 'succeeded', 'failed')),
    CONSTRAINT ck_tasks_attempts CHECK (max_attempts BETWEEN 1 AND 3 AND attempt_count BETWEEN 0
        AND max_attempts),
    CONSTRAINT ck_tasks_version CHECK (version >= 1),
    CONSTRAINT ck_tasks_completion CHECK ((status IN ('succeeded', 'failed')) = (completed_at IS
        NOT NULL)),
    CONSTRAINT ck_tasks_time CHECK (updated_at >= created_at AND completed_at >= created_at),
    CONSTRAINT ck_tasks_assignment CHECK (jsonb_typeof(assignment) = 'object' AND
        octet_length(assignment::text) <= 8192)
)
        """
    )
    op.execute(
        """CREATE TABLE agent_task_attempts (
    id UUID NOT NULL,
    investigation_run_id UUID NOT NULL,
    task_id UUID NOT NULL,
    investigation_attempt_id UUID NOT NULL,
    attempt_number INTEGER NOT NULL,
    start_version INTEGER NOT NULL,
    status VARCHAR(24) NOT NULL,
    outcome JSONB,
    started_at TIMESTAMP WITH TIME ZONE NOT NULL,
    lease_expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
    finished_at TIMESTAMP WITH TIME ZONE,
    PRIMARY KEY (id),
    CONSTRAINT fk_task_attempts_task_run FOREIGN KEY(task_id, investigation_run_id) REFERENCES
        agent_tasks (id, investigation_run_id) ON DELETE RESTRICT,
    CONSTRAINT fk_task_attempts_parent_run FOREIGN KEY(investigation_attempt_id,
        investigation_run_id) REFERENCES investigation_attempts (id, investigation_run_id) ON
        DELETE RESTRICT,
    CONSTRAINT uq_task_attempts_number UNIQUE (task_id, attempt_number),
    CONSTRAINT uq_task_attempts_start_version UNIQUE (task_id, start_version),
    CONSTRAINT ck_task_attempts_status CHECK (status IN ('running', 'succeeded', 'failed',
        'lease_expired')),
    CONSTRAINT ck_task_attempts_numbers CHECK (attempt_number BETWEEN 1 AND 3 AND start_version
        >= 1),
    CONSTRAINT ck_task_attempts_completion CHECK ((status = 'running') = (finished_at IS NULL)),
    CONSTRAINT ck_task_attempts_time CHECK (lease_expires_at > started_at AND finished_at >=
        started_at),
    CONSTRAINT ck_task_attempts_outcome CHECK ((status IN ('running', 'lease_expired') AND
        outcome IS NULL) OR (status IN ('succeeded', 'failed') AND outcome IS NOT NULL AND
        jsonb_typeof(outcome) = 'object' AND outcome ? 'status' AND outcome->>'status' = status
        AND octet_length(outcome::text) <= 16384))
)
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_task_attempts_running ON agent_task_attempts (task_id) "
        "WHERE status = 'running'"
    )


def downgrade() -> None:
    """按依赖顺序回退本切片表结构。"""
    op.drop_table("agent_task_attempts")
    op.drop_table("agent_tasks")
    op.drop_table("investigation_rounds")
    op.drop_constraint("uq_investigation_attempts_id_run", "investigation_attempts", type_="unique")
