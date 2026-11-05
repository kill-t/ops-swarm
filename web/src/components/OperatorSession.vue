<script setup lang="ts">
import { ref } from "vue";
import { requestJson } from "../services/api";
import {
  clearOperatorSession,
  operatorIdentity,
  setOperatorSession,
  type OperatorIdentity,
} from "../services/operator-session";

const token = ref("");
const loading = ref(false);
const error = ref("");

async function signIn(): Promise<void> {
  const value = token.value.trim();
  token.value = "";
  if (!value || loading.value) return;
  loading.value = true;
  error.value = "";
  try {
    const principal = await requestJson<OperatorIdentity>("/api/v1/operators/me", {
      authorization: `Bearer ${value}`,
    });
    if (!principal.permissions.includes("read")) throw new Error("该凭据没有协作台读取权限");
    setOperatorSession(value, principal);
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "身份确认失败";
  } finally {
    loading.value = false;
  }
}
</script>

<template>
  <section aria-label="可信操作者">
    <div v-if="operatorIdentity">
      {{ operatorIdentity.subject_id }} · {{ operatorIdentity.role }}
      <button type="button" @click="clearOperatorSession">退出</button>
    </div>
    <form v-else @submit.prevent="signIn">
      <label>操作者凭据 <input v-model="token" type="password" autocomplete="off" required /></label>
      <button :disabled="loading" type="submit">验证身份</button>
    </form>
    <p v-if="error" role="alert">{{ error }}</p>
  </section>
</template>
