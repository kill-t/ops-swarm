"""为同一操作的多次调用保存兼容历史键的槽位。"""

from alembic import op

revision = "0037_call_invocation_index"
down_revision = "0036_task_context_manifest"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
      ALTER TABLE investigation_call_reservations
        ADD COLUMN invocation_index INTEGER NOT NULL DEFAULT 0;
      ALTER TABLE investigation_call_reservations
        ADD CONSTRAINT ck_call_reservations_invocation_index
          CHECK (invocation_index BETWEEN 0 AND 99),
        ADD CONSTRAINT ck_call_reservations_invocation_key CHECK
          (idempotency_key = investigation_attempt_id::text || ':' ||
           COALESCE(task_attempt_id::text, 'root') || ':' || operation ||
           CASE WHEN invocation_index = 0 THEN '' ELSE ':' || invocation_index::text END);
    """)


def downgrade() -> None:
    op.execute("""
      DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM investigation_call_reservations WHERE invocation_index <> 0)
        THEN RAISE EXCEPTION '不能删除已保存的非零调用槽位';
        END IF;
      END $$;
      ALTER TABLE investigation_call_reservations
        DROP CONSTRAINT ck_call_reservations_invocation_key,
        DROP CONSTRAINT ck_call_reservations_invocation_index,
        DROP COLUMN invocation_index;
    """)
