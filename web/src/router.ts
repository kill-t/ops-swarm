import { createRouter, createWebHistory } from "vue-router";

import IncidentWorkroomView from "./views/IncidentWorkroomView.vue";

export const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: "/", redirect: "/incidents" },
    {
      path: "/incidents/:incidentId?",
      name: "incidents",
      component: IncidentWorkroomView,
    },
  ],
});
