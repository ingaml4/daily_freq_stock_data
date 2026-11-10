<script setup lang="ts">
import { computed } from "vue";
import { useRoute } from "vue-router";
import {
  NConfigProvider,
  NGlobalStyle,
  NMessageProvider,
  darkTheme,
  zhCN,
  dateZhCN,
} from "naive-ui";
import type { GlobalThemeOverrides } from "naive-ui";
import AppShell from "./components/AppShell.vue";

const route = useRoute();
const standalone = computed(() => route.meta.standalone === true);
const theme: GlobalThemeOverrides = {
  common: {
    primaryColor: "#64c9cf",
    primaryColorHover: "#8cdee1",
    primaryColorPressed: "#46b1b8",
    primaryColorSuppl: "#64c9cf",
    bodyColor: "#0c1420",
    cardColor: "#121f2e",
    modalColor: "#142233",
    popoverColor: "#182738",
    borderColor: "#29384a",
    textColorBase: "#e5edf5",
    textColor1: "#eef4f9",
    textColor2: "#c0cdda",
    textColor3: "#8394a7",
    fontFamily:
      'Inter, -apple-system, BlinkMacSystemFont, "PingFang SC", "Microsoft YaHei", sans-serif',
    fontSize: "14px",
    borderRadius: "8px",
  },
  DataTable: {
    thColor: "#152334",
    tdColor: "#111e2d",
    tdColorHover: "#192a3b",
    tdColorStriped: "#132131",
    borderColor: "#263649",
    thFontWeight: "500",
  },
};
</script>

<template>
  <n-config-provider
    :theme="darkTheme"
    :theme-overrides="theme"
    :locale="zhCN"
    :date-locale="dateZhCN"
  >
    <n-global-style />
    <n-message-provider>
      <router-view v-if="standalone" />
      <app-shell v-else><router-view /></app-shell>
    </n-message-provider>
  </n-config-provider>
</template>
