"""为现场 v2 固定观测截止时间及取证策略，不回填历史运行。"""

from alembic import op

revision = "0035_live_task_evidence"
down_revision = "0034_task_evidence_snapshot"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
      ALTER TABLE investigation_runs ADD COLUMN observation_cutoff_at TIMESTAMPTZ;
      ALTER TABLE investigation_runs DROP CONSTRAINT ck_investigation_runs_workflow_version;
      ALTER TABLE investigation_runs ADD CONSTRAINT ck_investigation_runs_workflow_version CHECK
        (workflow_version IN ('v1', 'v2') AND (workflow_version = 'v1' OR mode = 'multi_agent'));
      ALTER TABLE investigation_runs DROP CONSTRAINT ck_investigation_runs_evidence_policy;
      ALTER TABLE investigation_runs ADD CONSTRAINT ck_investigation_runs_evidence_policy CHECK
        (evidence_policy_version IS NULL OR (workflow_version = 'v2' AND call_budget IS NOT NULL
         AND ((evidence_policy_version = 'task-evidence-v1' AND data_source_mode = 'frozen') OR
              (evidence_policy_version = 'task-evidence-live-v1' AND data_source_mode = 'live'))));
      ALTER TABLE investigation_runs ADD CONSTRAINT ck_investigation_runs_observation_cutoff CHECK
        ((workflow_version = 'v2' AND data_source_mode = 'live' AND
          observation_cutoff_at IS NOT NULL AND evidence_policy_version IS NOT NULL AND
          evidence_policy_version = 'task-evidence-live-v1' AND
          observation_cutoff_at = date_trunc('second', observation_cutoff_at) AND
          observation_cutoff_at <= created_at AND
          observation_cutoff_at >= TIMESTAMPTZ '1970-01-01 06:00:00+00') OR
         ((workflow_version <> 'v2' OR data_source_mode <> 'live') AND
          observation_cutoff_at IS NULL));
    """)


def downgrade() -> None:
    op.execute("""
      DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM investigation_runs
                   WHERE workflow_version = 'v2' AND data_source_mode = 'live') THEN
          RAISE EXCEPTION '不能删除已固定的现场取证窗口';
        END IF;
      END $$;
      ALTER TABLE investigation_runs DROP CONSTRAINT ck_investigation_runs_observation_cutoff;
      ALTER TABLE investigation_runs DROP COLUMN observation_cutoff_at;
      ALTER TABLE investigation_runs DROP CONSTRAINT ck_investigation_runs_evidence_policy;
      ALTER TABLE investigation_runs ADD CONSTRAINT ck_investigation_runs_evidence_policy CHECK
        (evidence_policy_version IS NULL OR (evidence_policy_version = 'task-evidence-v1'
         AND workflow_version = 'v2' AND call_budget IS NOT NULL));
      ALTER TABLE investigation_runs DROP CONSTRAINT ck_investigation_runs_workflow_version;
      ALTER TABLE investigation_runs ADD CONSTRAINT ck_investigation_runs_workflow_version CHECK
        (workflow_version IN ('v1', 'v2') AND
         (workflow_version = 'v1' OR (mode = 'multi_agent' AND data_source_mode = 'frozen')));
    """)
