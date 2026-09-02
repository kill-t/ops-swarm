<script setup lang="ts">
import { RouterLink, RouterView } from "vue-router";
</script>

<template>
  <div class="app-shell">
    <header class="app-header">
      <div class="brand-block">
        <div class="brand-mark">OS</div>
        <div>
          <p>OpsSwarm</p>
          <span>Evidence-driven incident response</span>
        </div>
      </div>
      <nav class="app-nav" aria-label="主导航">
        <RouterLink to="/incidents">事故作战室</RouterLink>
        <RouterLink to="/approvals">审批中心</RouterLink>
        <RouterLink to="/executions">受控执行</RouterLink>
        <RouterLink to="/evaluations">评测面板</RouterLink>
      </nav>
      <div class="environment-badge">
        <span />
        LOCAL FAULT LAB
      </div>
    </header>
    <RouterView />
  </div>
</template>
