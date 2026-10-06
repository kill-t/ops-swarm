"""冻结日志 MCP 取证策略。

Revision ID: 0058_telemetry_mcp_log_policy
Revises: 0057_third_round_routing
Create Date: 2026-10-04
"""

from alembic import op

revision = "0058_telemetry_mcp_log_policy"
down_revision = "0057_third_round_routing"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE investigation_runs
          DROP CONSTRAINT ck_investigation_runs_observation_cutoff,
          DROP CONSTRAINT ck_investigation_runs_evidence_policy;
        ALTER TABLE investigation_runs
          ADD CONSTRAINT ck_investigation_runs_observation_cutoff CHECK
          ((workflow_version = 'v2' AND data_source_mode = 'live' AND
            observation_cutoff_at IS NOT NULL AND evidence_policy_version IS NOT NULL AND
            evidence_policy_version IN
              ('task-evidence-live-v1', 'task-evidence-live-mcp-log-v1') AND
            observation_cutoff_at = date_trunc('second', observation_cutoff_at) AND
            observation_cutoff_at <= created_at AND
            observation_cutoff_at >= TIMESTAMPTZ '1970-01-01 06:00:00+00') OR
           ((workflow_version <> 'v2' OR data_source_mode <> 'live') AND
            observation_cutoff_at IS NULL)),
          ADD CONSTRAINT ck_investigation_runs_evidence_policy CHECK
          (evidence_policy_version IS NULL OR
           (workflow_version IN ('v2', 'v3') AND call_budget IS NOT NULL AND
            ((evidence_policy_version = 'task-evidence-v1' AND
              data_source_mode = 'frozen') OR
             (evidence_policy_version IN
                ('task-evidence-live-v1', 'task-evidence-live-mcp-log-v1') AND
              data_source_mode = 'live'))));
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (
            SELECT 1 FROM investigation_runs
            WHERE evidence_policy_version = 'task-evidence-live-mcp-log-v1'
          ) THEN
            RAISE EXCEPTION
              'cannot downgrade while telemetry MCP log policy runs exist';
          END IF;
        END $$;
        ALTER TABLE investigation_runs
          DROP CONSTRAINT ck_investigation_runs_observation_cutoff,
          DROP CONSTRAINT ck_investigation_runs_evidence_policy;
        ALTER TABLE investigation_runs
          ADD CONSTRAINT ck_investigation_runs_observation_cutoff CHECK
          ((workflow_version = 'v2' AND data_source_mode = 'live' AND
            observation_cutoff_at IS NOT NULL AND evidence_policy_version IS NOT NULL AND
            evidence_policy_version = 'task-evidence-live-v1' AND
            observation_cutoff_at = date_trunc('second', observation_cutoff_at) AND
            observation_cutoff_at <= created_at AND
            observation_cutoff_at >= TIMESTAMPTZ '1970-01-01 06:00:00+00') OR
           ((workflow_version <> 'v2' OR data_source_mode <> 'live') AND
            observation_cutoff_at IS NULL)),
          ADD CONSTRAINT ck_investigation_runs_evidence_policy CHECK
          (evidence_policy_version IS NULL OR
           (workflow_version IN ('v2', 'v3') AND call_budget IS NOT NULL AND
            ((evidence_policy_version = 'task-evidence-v1' AND
              data_source_mode = 'frozen') OR
             (evidence_policy_version = 'task-evidence-live-v1' AND
              data_source_mode = 'live'))));
        """
    )
