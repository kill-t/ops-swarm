"""保存模型预算配置、完整应用输入和逐次用量。"""

from alembic import op

revision = "0033_model_input_budget"
down_revision = "0032_shared_call_budget"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
      ALTER TABLE investigation_runs ADD COLUMN model_budget JSONB;
      ALTER TABLE investigation_runs ADD CONSTRAINT ck_investigation_runs_model_budget CHECK
        (model_budget IS NULL OR (workflow_version = 'v2' AND call_budget IS NOT NULL AND
         jsonb_typeof(model_budget) = 'object' AND octet_length(model_budget::text) <= 8192));
      ALTER TABLE investigation_call_reservations
        ADD COLUMN model_input JSONB, ADD COLUMN model_allocation JSONB,
        ADD COLUMN model_usage JSONB;
      ALTER TABLE investigation_call_reservations
        ADD CONSTRAINT ck_call_reservations_model_budget CHECK
        ((model_input IS NULL) = (model_allocation IS NULL) AND
         (model_allocation IS NULL OR (kind = 'model' AND
          jsonb_typeof(model_input) = 'object' AND octet_length(model_input::text) <= 524288 AND
          jsonb_typeof(model_allocation) = 'object' AND
          octet_length(model_allocation::text) <= 8192)) AND
         (model_usage IS NULL OR (model_allocation IS NOT NULL AND status <> 'reserved' AND
          jsonb_typeof(model_usage) = 'object' AND octet_length(model_usage::text) <= 8192)));
    """)


def downgrade() -> None:
    op.execute("""
      DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM investigation_runs WHERE model_budget IS NOT NULL) THEN
          RAISE EXCEPTION '不能删除已启用的模型预算与输入审计事实';
        END IF;
      END $$;
      ALTER TABLE investigation_call_reservations DROP CONSTRAINT ck_call_reservations_model_budget;
      ALTER TABLE investigation_call_reservations
        DROP COLUMN model_input, DROP COLUMN model_allocation, DROP COLUMN model_usage;
      ALTER TABLE investigation_runs DROP CONSTRAINT ck_investigation_runs_model_budget;
      ALTER TABLE investigation_runs DROP COLUMN model_budget;
    """)
