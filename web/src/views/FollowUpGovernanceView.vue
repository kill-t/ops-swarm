<script setup lang="ts">
import { onMounted, onUnmounted, ref } from "vue";

import FollowUpGovernanceList from "../components/FollowUpGovernanceList.vue";
import { useFollowUpGovernanceStore } from "../stores/follow-up-governance-store";

const store = useFollowUpGovernanceStore();
const ownerInput = ref("");
const overdueInput = ref(false);

async function applyFilters(): Promise<void> {
  await store.load(ownerInput.value, overdueInput.value);
}

async function resetFilters(): Promise<void> {
  ownerInput.value = "";
  overdueInput.value = false;
  await store.load();
}

async function refreshApplied(): Promise<void> {
  await store.load(store.ownerId ?? "", store.overdueOnly);
}

onMounted(() => store.load());
onUnmounted(() => store.clear());
</script>

<template>
  <main class="follow-up-governance-view">
    <header class="governance-hero">
      <div>
        <p class="eyebrow">POST-INCIDENT GOVERNANCE</p>
        <h1>改进行动治理中心</h1>
        <p>跨事故查看未完成责任项；完成动作仍需回到具体事故复盘上下文。</p>
      </div>
      <span>READ ONLY INDEX</span>
    </header>

    <form class="governance-filters" @submit.prevent="applyFilters">
      <label>
        负责人精确标识
        <input
          v-model="ownerInput"
          name="governance-owner"
          maxlength="100"
          autocomplete="off"
          placeholder="例如 sre-001"
        />
      </label>
      <label class="governance-checkbox">
        <input v-model="overdueInput" name="governance-overdue" type="checkbox" />
        仅查看服务器判定已逾期
      </label>
      <div class="governance-filter-actions">
        <button class="decision-button" type="submit" :disabled="store.loading">应用筛选</button>
        <button class="icon-button" type="button" :disabled="store.loading" @click="resetFilters">
          清除
        </button>
      </div>
    </form>

    <p class="governance-boundary">
      这里不提供批量完成或重新分配。请进入来源事故，核对冻结报告和当前行动项版本后再提交人工动作。
    </p>

    <FollowUpGovernanceList
      :items="store.items"
      :total="store.total"
      :as-of="store.asOf"
      :loading="store.loading"
      :error="store.error"
      @refresh="refreshApplied"
    />
  </main>
</template>
