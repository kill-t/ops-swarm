"""固定新运行的上下文策略，并随调用预留保存材料清单。"""

from alembic import op

revision = "0036_task_context_manifest"
down_revision = "0035_live_task_evidence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
      ALTER TABLE investigation_runs ADD COLUMN context_policy_version VARCHAR(32);
      ALTER TABLE investigation_runs ADD CONSTRAINT ck_investigation_runs_context_policy CHECK
        (context_policy_version IS NULL OR (context_policy_version = 'task-context-v1' AND
         workflow_version = 'v2' AND model_budget IS NOT NULL AND
         evidence_policy_version IS NOT NULL));
      ALTER TABLE investigation_call_reservations ADD COLUMN context_manifest JSONB;
      ALTER TABLE investigation_call_reservations
        ADD CONSTRAINT ck_call_reservations_context_manifest CHECK
        (context_manifest IS NULL OR (model_input IS NOT NULL AND kind = 'model' AND
         jsonb_typeof(context_manifest) = 'object' AND
         octet_length(context_manifest::text) <= 32768));
    """)


def downgrade() -> None:
    op.execute("""
      DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM investigation_runs WHERE context_policy_version IS NOT NULL)
           OR EXISTS (SELECT 1 FROM investigation_call_reservations
                       WHERE context_manifest IS NOT NULL) THEN
          RAISE EXCEPTION '不能删除已固定的上下文策略或调用材料清单';
        END IF;
      END $$;
      ALTER TABLE investigation_call_reservations
        DROP CONSTRAINT ck_call_reservations_context_manifest;
      ALTER TABLE investigation_call_reservations DROP COLUMN context_manifest;
      ALTER TABLE investigation_runs DROP CONSTRAINT ck_investigation_runs_context_policy;
      ALTER TABLE investigation_runs DROP COLUMN context_policy_version;
    """)
