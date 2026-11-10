<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { useRoute } from "vue-router";

const route = useRoute();
const expanded = ref(false);
const links = [
  {
    label: "信号发现",
    to: "/signals/vol_spike",
    section: "/signals",
    description: "价量与技术信号",
  },
  {
    label: "收盘复盘",
    to: "/summary",
    section: "/summary",
    description: "当日市场与选股依据",
  },
  {
    label: "梯队跟踪",
    to: "/tiers",
    section: "/tiers",
    description: "名单与后续表现",
  },
  {
    label: "策略验证",
    to: "/strategy-review",
    section: "/strategy-review",
    description: "规则比较与成交约束",
  },
];
const activeLabel = computed(
  () =>
    links.find((item) => route.path.startsWith(item.section))?.label ||
    "信号发现",
);
watch(
  () => route.fullPath,
  () => {
    expanded.value = false;
  },
);
</script>

<template>
  <div class="workspace">
    <header class="mobile-top">
      <router-link class="mobile-brand" to="/signals/vol_spike"
        >行情工作台</router-link
      >
      <button
        class="menu-toggle"
        type="button"
        :aria-expanded="expanded"
        aria-controls="workspace-nav"
        @click="expanded = !expanded"
      >
        {{ expanded ? "收起导航" : activeLabel + " · 导航" }}
      </button>
    </header>
    <aside class="sidebar" :class="{ 'is-open': expanded }">
      <router-link
        class="brand"
        to="/signals/vol_spike"
        aria-label="行情工作台首页"
      >
        <span class="brand-mark" aria-hidden="true"><i></i><i></i><i></i></span>
        <span>行情工作台</span>
      </router-link>
      <nav id="workspace-nav" class="primary-nav" aria-label="主导航">
        <router-link
          v-for="item in links"
          :key="item.to"
          :to="item.to"
          class="nav-item"
          :class="{ active: route.path.startsWith(item.section) }"
          :aria-current="
            route.path.startsWith(item.section) ? 'page' : undefined
          "
        >
          <span class="nav-label">{{ item.label }}</span>
          <span class="nav-description">{{ item.description }}</span>
        </router-link>
      </nav>
      <div class="sidebar-footer">
        <span>沪深 A 股 · 北京时间</span>
        <a href="/logout">退出登录</a>
      </div>
    </aside>
    <main class="workspace-main" id="main-content">
      <div class="page-content"><slot /></div>
    </main>
  </div>
</template>
