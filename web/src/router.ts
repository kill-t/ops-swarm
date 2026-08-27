import { createRouter, createWebHistory } from "vue-router";

import IncidentWorkroomView from "./views/IncidentWorkroomView.vue";
import EvaluationDashboardView from "./views/EvaluationDashboardView.vue";

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
      path: "/evaluations/:runId?",
      name: "evaluations",
      component: EvaluationDashboardView,
    },
  ],
});
