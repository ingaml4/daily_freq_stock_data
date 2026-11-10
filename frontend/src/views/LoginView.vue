<script setup lang="ts">
import { onMounted, ref } from "vue";
import { useRoute } from "vue-router";
import { NAlert, NButton, NCheckbox, NInput } from "naive-ui";
import { safeNext } from "../api";

const route = useRoute();
const password = ref("");
const remember = ref(true);
const busy = ref(false);
const error = ref("");

onMounted(async () => {
  try {
    const response = await fetch("/api/session", {
      credentials: "same-origin",
      headers: { Accept: "application/json" },
    });
    if (!response.ok) return;
    const session = (await response.json()) as { authenticated?: boolean };
    if (session.authenticated)
      window.location.replace(safeNext(route.query.next));
  } catch {
    // 会话探测失败时，仍允许用户通过表单重新登录。
  }
});

async function submit() {
  if (!password.value || busy.value) return;
  busy.value = true;
  error.value = "";
  const next = safeNext(route.query.next);
  const body = new URLSearchParams({ password: password.value, next });
  if (remember.value) body.set("remember", "1");
  try {
    const response = await fetch("/api/session", {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/x-www-form-urlencoded",
        Accept: "application/json",
      },
      body,
    });
    const result = (await response.json()) as {
      ok?: boolean;
      next?: string;
      error?: string;
    };
    if (!response.ok || !result.ok) {
      error.value = result.error || "登录未成功，请重试";
      return;
    }
    window.location.assign(safeNext(result.next || next));
  } catch {
    error.value = "暂时无法连接服务，请稍后重试";
  } finally {
    busy.value = false;
  }
}
</script>

<template>
  <main class="login-page">
    <section class="login-panel" aria-labelledby="login-heading">
      <div class="login-brand">
        <span class="brand-mark" aria-hidden="true"><i></i><i></i><i></i></span
        >行情工作台
      </div>
      <h1 id="login-heading">登录你的工作台</h1>
      <p class="login-intro">查看市场信号、收盘复盘与策略表现。</p>
      <form @submit.prevent="submit">
        <label class="field-label" for="workspace-password">访问口令</label>
        <n-input
          id="workspace-password"
          v-model:value="password"
          type="password"
          show-password-on="click"
          placeholder="输入访问口令"
          size="large"
          autofocus
          :input-props="{ autocomplete: 'current-password', name: 'password' }"
        />
        <n-checkbox v-model:checked="remember" class="login-remember"
          >保持登录 7 天</n-checkbox
        >
        <n-alert
          v-if="error"
          type="error"
          :show-icon="false"
          class="login-error"
          >{{ error }}</n-alert
        >
        <n-button
          attr-type="submit"
          type="primary"
          size="large"
          block
          :loading="busy"
          :disabled="!password"
          >进入工作台</n-button
        >
      </form>
      <p class="login-footer">沪深 A 股观察与研究</p>
    </section>
  </main>
</template>
