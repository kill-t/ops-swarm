import { createRouter, createWebHistory } from "vue-router";

import IncidentWorkroomView from "./views/IncidentWorkroomView.vue";
import EvaluationDashboardView from "./views/EvaluationDashboardView.vue";
import ApprovalCenterView from "./views/ApprovalCenterView.vue";
import ExecutionCenterView from "./views/ExecutionCenterView.vue";

export const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: "/", redirect: "/incidents" },
    {
      path: "/incidents/:incidentId?",
      name: "incidents",
      component: IncidentWorkroomView,
    },
    {
      path: "/approvals/:requestId?",
      name: "approvals",
      component: ApprovalCenterView,
    },
    {
      path: "/executions/:planId?",
      name: "executions",
      component: ExecutionCenterView,
    },
    {
      path: "/evaluations/:runId?",
      name: "evaluations",
      component: EvaluationDashboardView,
    },
  ],
});
