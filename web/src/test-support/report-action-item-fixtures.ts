import type {
  ReportActionItem,
  ReportActionItemGovernanceEntry,
} from "../types/report-action-items";

export const OPEN_REPORT_ACTION_ITEM: ReportActionItem = {
  id: "action-item-001",
  report_id: "report-001",
  idempotency_key: "follow-up-001",
  title: "补充 Redis 故障演练",
  description: "把依赖恢复和验证步骤加入每月演练。",
  owner_id: "sre-001",
  status: "open",
  version: 1,
  due_at: "2026-10-01T08:00:00Z",
  created_by_id: "engineer-001",
  created_at: "2026-09-04T08:00:00Z",
  completed_by_id: null,
  completion_note: null,
  completed_at: null,
};

export const COMPLETED_REPORT_ACTION_ITEM: ReportActionItem = {
  ...OPEN_REPORT_ACTION_ITEM,
  status: "completed",
  version: 2,
  completed_by_id: "engineer-002",
  completion_note: "演练脚本和恢复检查已经合入。",
  completed_at: "2026-09-08T08:30:00Z",
};

export const REPORT_ACTION_ITEM_GOVERNANCE_ENTRY: ReportActionItemGovernanceEntry = {
  action_item: OPEN_REPORT_ACTION_ITEM,
  incident: {
    id: "incident-001",
    display_number: 1,
    reference: "INC-000001",
    service_name: "order-service",
    title: "订单接口大量返回 500",
    severity: "p1",
  },
  overdue: true,
};
