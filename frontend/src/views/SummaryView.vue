<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { NAlert, NButton, NSelect, NTag } from "naive-ui";
import MarkdownIt from "markdown-it";
import DOMPurify from "dompurify";
import { getJson } from "../api";
import PageState from "../components/PageState.vue";
import { localTime, message } from "../components/presentation";

interface SummaryData {
  date: string | null;
  engine?: string;
  created_at?: string;
  content: string;
  dates: string[];
  coverage_state?: string;
  missing?: boolean;
  publication?: {
    reason?: string;
    market_date?: string;
    generated_at?: string;
  };
}
const route = useRoute();
const router = useRouter();
const data = ref<SummaryData | null>(null);
const loading = ref(true);
const error = ref("");
let controller: AbortController | undefined;
const markdown = new MarkdownIt({ html: false, breaks: false, linkify: false });
const html = computed(() =>
  DOMPurify.sanitize(markdown.render(data.value?.content || ""), {
    FORBID_TAGS: ["img"],
  }),
);
const dates = computed(() =>
  (data.value?.dates || []).map((date) => ({ label: date, value: date })),
);
const selectedDate = computed(() =>
  typeof route.query.date === "string"
    ? route.query.date
    : data.value?.date || null,
);
const isFallback = computed(() => !!data.value?.engine?.startsWith("rule"));
const coverageLabel = computed(
  () =>
    ({ ready: "数据完整", waiting: "正在生成", degraded: "部分依据待补充" })[
      data.value?.coverage_state || ""
    ] || "覆盖状态待核验",
);
const sourceUrl = computed(
  () => `/summary.md?date=${encodeURIComponent(data.value?.date || "")}`,
);

async function load() {
  controller?.abort();
  controller = new AbortController();
  const current = controller;
  loading.value = true;
  error.value = "";
  try {
    data.value = await getJson<SummaryData>(
      "/api/summary",
      route.query.date ? { date: String(route.query.date) } : undefined,
      current.signal,
    );
  } catch (e) {
    if (!current.signal.aborted) error.value = message(e);
  } finally {
    if (!current.signal.aborted) loading.value = false;
  }
}
function selectDate(date: string) {
  router.replace({ path: "/summary", query: { date } });
}
watch(() => route.query.date, load, { immediate: true });
onBeforeUnmount(() => controller?.abort());
</script>

<template>
  <div class="view-heading">
    <div>
      <h1 class="view-title">收盘复盘</h1>
      <p class="view-caption">把当天的市场表现、信号和候选理由放在一起回看。</p>
    </div>
    <div class="summary-actions">
      <n-select
        :value="selectedDate"
        :options="dates"
        placeholder="选择复盘日期"
        style="width: 160px"
        :disabled="!dates.length"
        @update:value="selectDate"
      /><n-button
        v-if="data?.content"
        tag="a"
        :href="sourceUrl"
        download
        secondary
        >下载原文</n-button
      >
    </div>
  </div>
  <page-state
    :loading="loading"
    :error="error"
    :empty="!data?.content"
    title="复盘尚未发布"
    :description="
      selectedDate
        ? `${selectedDate} 暂无已发布复盘，可选择其他日期。`
        : '盘后任务完成后，复盘会显示在这里。'
    "
    @retry="load"
  >
    <n-alert v-if="isFallback" type="warning" :show-icon="false" class="section"
      >当前是规则版复盘。{{
        data?.publication?.reason || "AI 复盘暂未生成，具体状态可查看数据覆盖。"
      }}</n-alert
    >
    <section class="panel summary-paper">
      <div class="summary-byline">
        <span class="summary-date tabular">{{ data?.date }}</span
        ><n-tag
          size="small"
          :bordered="false"
          :type="isFallback ? 'warning' : 'info'"
          >{{ isFallback ? "规则版" : "AI 复盘" }}</n-tag
        ><a :href="`/api/readiness?date=${data?.date}`" class="coverage-link">{{
          coverageLabel
        }}</a
        ><span class="data-note">发布于 {{ localTime(data?.created_at) }}</span>
      </div>
      <article class="report-prose" v-html="html" />
      <footer class="summary-foot data-note">
        内容保留原始发布版本 · {{ data?.engine || "来源未记录" }} · 北京时间
      </footer>
    </section>
  </page-state>
</template>

<style scoped>
.summary-actions {
  display: flex;
  gap: 10px;
  align-items: center;
}
.summary-paper {
  margin-top: 24px;
  padding: 28px 34px;
}
.summary-byline {
  display: flex;
  align-items: center;
  gap: 13px;
  flex-wrap: wrap;
  border-bottom: 1px solid var(--color-border);
  padding-bottom: 20px;
  margin-bottom: 24px;
}
.summary-date {
  font-size: 17px;
  font-weight: 600;
}
.coverage-link {
  font-size: 12px;
  color: var(--color-accent);
  text-decoration: none;
}
.summary-byline > .data-note {
  margin-left: auto;
}
.summary-foot {
  border-top: 1px solid var(--color-border);
  padding-top: 20px;
  margin-top: 28px;
}
.report-prose {
  max-width: 1020px;
  font-size: 14px;
  line-height: 1.95;
  color: var(--color-text);
}
.report-prose :deep(h1) {
  font-size: 24px;
  line-height: 1.5;
  margin: 20px 0;
}
.report-prose :deep(h2) {
  font-size: 18px;
  margin: 30px 0 13px;
  font-weight: 600;
}
.report-prose :deep(h3) {
  font-size: 15px;
  margin: 23px 0 10px;
}
.report-prose :deep(p) {
  margin: 10px 0;
}
.report-prose :deep(ul),
.report-prose :deep(ol) {
  padding-left: 24px;
}
.report-prose :deep(li) {
  margin: 5px 0;
}
.report-prose :deep(a) {
  color: var(--color-accent);
}
.report-prose :deep(blockquote) {
  margin: 18px 0;
  border-left: 2px solid var(--color-accent);
  padding: 3px 18px;
  color: var(--color-text-muted);
  background: var(--color-surface-raised);
}
.report-prose :deep(table) {
  display: block;
  max-width: 100%;
  overflow: auto;
  border-collapse: collapse;
  margin: 18px 0;
  font-size: 13px;
}
.report-prose :deep(th),
.report-prose :deep(td) {
  padding: 9px 12px;
  border: 1px solid var(--color-border);
  text-align: left;
  white-space: nowrap;
}
.report-prose :deep(code) {
  font-size: 12px;
  background: var(--color-surface-raised);
  padding: 2px 5px;
  border-radius: 3px;
}
.report-prose :deep(pre) {
  overflow: auto;
  padding: 14px;
  background: var(--color-bg);
  border: 1px solid var(--color-border);
  border-radius: 7px;
}
.report-prose :deep(pre code) {
  padding: 0;
  background: transparent;
}
.report-prose :deep(hr) {
  border: 0;
  border-top: 1px solid var(--color-border);
  margin: 24px 0;
}
@media (max-width: 700px) {
  .summary-paper {
    padding: 20px 17px;
    margin-top: 18px;
  }
  .summary-byline > .data-note {
    margin-left: 0;
    width: 100%;
  }
  .summary-actions {
    width: 100%;
  }
  .report-prose {
    font-size: 13px;
  }
}
</style>
