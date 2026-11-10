<script setup lang="ts">
import { computed, h, onBeforeUnmount, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import {
  NAlert,
  NButton,
  NCheckbox,
  NCheckboxGroup,
  NDataTable,
  NInput,
  NPagination,
  NPopover,
  NSelect,
  type DataTableColumns,
  type DataTableSortState,
} from "naive-ui";
import { getJson } from "../api";
import PageState from "../components/PageState.vue";
import {
  change,
  message,
  number,
  numeric,
  percent,
  stockLink,
} from "../components/presentation";

type Row = {
  secid: string;
  code: string;
  name: string;
  board: string;
  board_group: string;
  industry: string;
  url: string;
  [key: string]: unknown;
};
interface SignalData {
  view: string;
  date: string | null;
  today: string;
  rows: Row[];
  total: number;
  stale: boolean;
  stale_reason: string;
  cache_written: string;
  update_note: string;
  columns?: string[];
  title?: string;
  help?: string;
  error?: string;
  limit?: number;
}
interface ViewInfo {
  key: string;
  nav: string;
  title?: string;
}
const viewOptions: ViewInfo[] = [
  { key: "vol_spike", nav: "放量上攻" },
  { key: "macd_golden", nav: "MACD 金叉" },
  { key: "kdj_golden", nav: "KDJ 金叉" },
  { key: "boll_break_lower", nav: "布林下轨" },
  { key: "reversal", nav: "底部反转" },
  { key: "box_range", nav: "放量突破箱体" },
];
const route = useRoute();
const router = useRouter();
const activeView = computed(() =>
  typeof route.params.view === "string" &&
  viewOptions.some((v) => v.key === route.params.view)
    ? route.params.view
    : "vol_spike",
);
const data = ref<SignalData | null>(null);
const loading = ref(true);
const error = ref("");
const search = ref(String(route.query.q || ""));
const board = ref("all");
const page = ref(1);
const pageSize = ref(20);
const customColumns = ref<string[] | null>(null);
function setColumns(value: Array<string | number>) {
  customColumns.value = value.map(String);
}
let controller: AbortController | undefined;

const viewFields: Record<string, string[]> = {
  vol_spike: ["vol_ratio_5", "vol_ratio_20", "vol_z60", "vol_signal"],
  macd_golden: ["macd_diff", "macd_dea", "macd_bar"],
  kdj_golden: ["kdj_k", "kdj_d", "kdj_j"],
  boll_break_lower: ["boll_pos", "boll_lower", "boll_mid", "boll_upper"],
  reversal: [
    "rev_score",
    "doji_vol_ratio",
    "doji_drawdown",
    "doji_stop_pos",
    "rev_signals",
  ],
  box_range: [
    "box_high",
    "box_low",
    "box_breakout_pct",
    "vol_ratio_5",
    "vol_ratio_20",
    "box_width",
  ],
};
const fieldNames: Record<string, string> = {
  code: "代码",
  name: "名称",
  industry: "行业",
  board: "板块",
  close: "收盘价",
  pct_chg: "涨跌幅",
  amount_yi: "成交额 / 亿",
  turnover: "换手率",
  vol_ratio_5: "5 日量比",
  vol_ratio_20: "20 日量比",
  valid_bars: "量能样本数",
  reversal_bars: "反转样本数",
  vol_z60: "Z60",
  vol_signal: "量能形态",
  macd_diff: "DIFF",
  macd_dea: "DEA",
  macd_bar: "MACD 柱",
  kdj_k: "K",
  kdj_d: "D",
  kdj_j: "J",
  boll_pos: "布林位置",
  boll_lower: "布林下轨",
  boll_mid: "布林中轨",
  boll_upper: "布林上轨",
  rev_score: "反转评分",
  doji_vol_ratio: "量 / 前 5 日均",
  doji_drawdown: "距高点回撤",
  doji_stop_pos: "收盘位置",
  doji_pos: "60 日区间位置",
  doji_body: "实体比例",
  doji_lower: "下影比例",
  doji_upper: "上影比例",
  rev_signals: "反转依据",
  box_high: "箱体上沿",
  box_low: "箱体下沿",
  box_breakout_pct: "突破幅度",
  box_width: "箱体宽度",
  box_pos: "相对原箱体位置",
  box_avg_turnover: "日均换手率",
  box_avg_amplitude: "日均振幅",
  ma5: "MA5",
  ma10: "MA10",
  ma20: "MA20",
  ma60: "MA60",
  signals_cn: "命中信号",
};
const fieldPrecision: Record<string, number> = {
  valid_bars: 0,
  reversal_bars: 0,
  rev_score: 0,
  vol_z60: 1,
  kdj_k: 1,
  kdj_d: 1,
  kdj_j: 1,
  ma5: 3,
  ma10: 3,
  ma20: 3,
  ma60: 3,
  macd_diff: 3,
  macd_dea: 3,
  macd_bar: 3,
};
const staleMessage = computed(() => {
  const date = data.value?.date;
  if (!date) return "尚无有效收盘数据，盘后更新完成后可在这里查看。";
  const times = [
    ...new Set(data.value?.update_note.match(/\b\d{2}:\d{2}\b/g) || []),
  ];
  const schedule = times.length
    ? `交易日计划于北京时间 ${times.join("、")} 更新。`
    : "数据在交易日盘后更新。";
  return `当前展示 ${date} 的最新收盘数据。${schedule}休市期间显示最近交易日的数据。`;
});

const defaultFields = computed(() => {
  const columns = data.value?.columns;
  if (columns?.length)
    return columns.filter((key) => !["code", "name"].includes(key));
  return [
    "industry",
    "board",
    "close",
    "pct_chg",
    "amount_yi",
    "turnover",
    ...(viewFields[activeView.value] || []),
  ];
});
const availableFields = computed(() => {
  const exposed = new Set(
    (data.value?.rows || []).flatMap((row) => Object.keys(row)),
  );
  const optional = ["ma5", "ma10", "ma20", "ma60", "signals_cn"].filter((key) =>
    exposed.has(key),
  );
  return [...new Set([...defaultFields.value, ...optional])];
});
const visibleFields = computed(
  () => customColumns.value || defaultFields.value,
);
const boardOptions = computed(() => [
  { label: "全部板块", value: "all" },
  ...[
    ...new Set(
      (data.value?.rows || [])
        .map((r) => r.board_group || r.board)
        .filter(Boolean),
    ),
  ].map((value) => ({ label: value, value })),
]);
const filteredRows = computed(() =>
  (data.value?.rows || []).filter(
    (r) => board.value === "all" || (r.board_group || r.board) === board.value,
  ),
);
const defaultSort = ref<{
  columnKey: string;
  order: "ascend" | "descend" | false;
}>({ columnKey: "pct_chg", order: false });
const sortedRows = computed(() => {
  const list = [...filteredRows.value];
  const sort = defaultSort.value;
  if (!sort.order) return list;
  return list.sort((a, b) => {
    const x = numeric(a[sort.columnKey]);
    const y = numeric(b[sort.columnKey]);
    if (x === null && y === null)
      return (
        String(a[sort.columnKey] || "").localeCompare(
          String(b[sort.columnKey] || ""),
          "zh-CN",
        ) * (sort.order === "ascend" ? 1 : -1)
      );
    if (x === null) return 1;
    if (y === null) return -1;
    return (x - y) * (sort.order === "ascend" ? 1 : -1);
  });
});
const displayedRows = computed(() =>
  sortedRows.value.slice(
    (page.value - 1) * pageSize.value,
    page.value * pageSize.value,
  ),
);
const tableColumns = computed<DataTableColumns<Row>>(() => [
  { title: "股票", key: "stock", fixed: "left", width: 154, render: stockLink },
  ...visibleFields.value.map((key) => ({
    title: fieldNames[key] || key,
    key,
    width:
      key === "rev_signals" || key === "signals_cn"
        ? 250
        : key === "industry"
          ? 120
          : 108,
    align: [
      "industry",
      "board",
      "rev_signals",
      "signals_cn",
      "vol_signal",
    ].includes(key)
      ? ("left" as const)
      : ("right" as const),
    sorter:
      key === "rev_signals" || key === "signals_cn"
        ? false
        : ("default" as const),
    sortOrder:
      defaultSort.value.columnKey === key ? defaultSort.value.order : false,
    render: (row: Row) => {
      if (key === "pct_chg") return change(row[key], false);
      if (key === "doji_drawdown") return change(row[key], true, 1);
      if (key === "box_breakout_pct") return change(row[key]);
      if (["doji_body", "doji_lower", "doji_upper"].includes(key))
        return percent(row[key]);
      if (key === "box_width") return percent(row[key], true, 1);
      if (["turnover", "box_avg_turnover", "box_avg_amplitude"].includes(key))
        return `${number(row[key])}${numeric(row[key]) === null ? "" : "%"}`;
      if (
        [
          "industry",
          "board",
          "rev_signals",
          "signals_cn",
          "vol_signal",
        ].includes(key)
      )
        return String(row[key] || "—");
      return h(
        "span",
        { class: "tabular" },
        number(row[key], fieldPrecision[key] ?? 2),
      );
    },
  })),
]);

async function load() {
  controller?.abort();
  controller = new AbortController();
  const current = controller;
  loading.value = true;
  error.value = "";
  try {
    data.value = await getJson<SignalData>(
      "/api/signals",
      { view: activeView.value, q: String(route.query.q || ""), limit: "300" },
      current.signal,
    );
    if (data.value.error) error.value = data.value.error;
    page.value = 1;
  } catch (e) {
    if (!current.signal.aborted) error.value = message(e);
  } finally {
    if (!current.signal.aborted) loading.value = false;
  }
}
function submitSearch() {
  router.replace({
    path: `/signals/${activeView.value}`,
    query: search.value.trim() ? { q: search.value.trim() } : {},
  });
}
function sortChanged(sort: DataTableSortState | DataTableSortState[] | null) {
  const state = Array.isArray(sort) ? sort[0] : sort;
  defaultSort.value = {
    columnKey: String(state?.columnKey || ""),
    order: state?.order || false,
  };
  page.value = 1;
}
watch(
  () => route.fullPath,
  () => {
    search.value = String(route.query.q || "");
    customColumns.value = null;
    defaultSort.value = { columnKey: "", order: false };
    load();
  },
  { immediate: true },
);
watch([board, pageSize], () => {
  page.value = 1;
});
onBeforeUnmount(() => controller?.abort());
</script>

<template>
  <div class="view-heading">
    <div>
      <h1 class="view-title">信号发现</h1>
      <p class="view-caption">从量价与技术形态中筛选值得跟踪的股票。</p>
    </div>
    <n-button secondary :loading="loading" @click="load">刷新数据</n-button>
  </div>
  <div class="signal-navigation">
    <div class="signal-tabs" aria-label="信号类型">
      <router-link
        v-for="view in viewOptions"
        :key="view.key"
        :to="`/signals/${view.key}`"
        :class="{ active: activeView === view.key }"
        >{{ view.nav }}</router-link
      >
    </div>
    <n-popover
      v-if="!loading && data?.help"
      trigger="click"
      placement="bottom-end"
      :show-arrow="false"
    >
      <template #trigger
        ><n-button quaternary size="small" class="rule-help-trigger"
          >筛选口径</n-button
        ></template
      >
      <div class="rule-help-copy">{{ data.help }}</div>
    </n-popover>
  </div>
  <p
    v-if="!loading && activeView === 'box_range' && data?.title"
    class="data-note signal-rule-title"
  >
    {{ data.title }}
  </p>
  <n-alert
    v-if="!loading && data?.stale"
    type="warning"
    :show-icon="false"
    class="section"
    >{{ staleMessage }}</n-alert
  >
  <section class="panel">
    <div class="signal-toolbar">
      <n-input
        v-model:value="search"
        clearable
        placeholder="搜索股票代码或名称"
        class="search-input"
        @keyup.enter="submitSearch"
        @clear="
          search = '';
          submitSearch();
        "
      />
      <n-button type="primary" @click="submitSearch">查询</n-button>
      <n-select
        v-model:value="board"
        :options="boardOptions"
        class="board-select"
      />
      <n-popover trigger="click" placement="bottom-end"
        ><template #trigger><n-button quaternary>显示列</n-button></template>
        <n-checkbox-group
          :value="visibleFields"
          class="column-options"
          @update:value="setColumns"
          ><n-checkbox
            v-for="key in availableFields"
            :key="key"
            :value="key"
            :label="fieldNames[key] || key"
        /></n-checkbox-group>
      </n-popover>
    </div>
    <div v-if="!loading && data" class="signal-meta data-note">
      <span
        ><strong class="tabular">{{ data.date || "—" }}</strong> 收盘数据</span
      ><span
        >{{ filteredRows.length }} 只{{
          data.total > (data.rows?.length || 0)
            ? `，当前载入 ${data.rows.length} / ${data.total}`
            : ""
        }}</span
      ><span v-if="data.cache_written">更新于 {{ data.cache_written }}</span>
    </div>
    <page-state
      :loading="loading"
      :error="error"
      :empty="!filteredRows.length"
      title="暂无命中股票"
      description="可切换其他信号，或调整搜索和板块条件。"
      @retry="load"
    >
      <n-data-table
        :columns="tableColumns"
        :data="displayedRows"
        :row-key="(row) => row.secid"
        :scroll-x="154 + visibleFields.length * 112"
        size="small"
        :bordered="false"
        @update:sorter="sortChanged"
      />
      <div class="table-footer">
        <span class="data-note">点击股票名称查看雪球行情</span
        ><n-pagination
          v-model:page="page"
          v-model:page-size="pageSize"
          :item-count="filteredRows.length"
          :page-sizes="[20, 50, 100]"
          show-size-picker
        />
      </div>
    </page-state>
  </section>
  <p v-if="data?.update_note" class="data-note section">
    {{ data.update_note }} · 所有日期与时间均为北京时间
  </p>
</template>

<style scoped>
.signal-navigation {
  display: flex;
  align-items: center;
  gap: 12px;
  margin: 24px 0 18px;
}
.rule-help-trigger {
  flex: 0 0 auto;
}
.rule-help-copy {
  width: min(440px, calc(100vw - 64px));
  max-height: min(60vh, 480px);
  overflow: auto;
  white-space: pre-wrap;
  font-size: 12px;
  line-height: 1.8;
}
.signal-rule-title {
  margin: -4px 0 18px;
}
.signal-tabs {
  scrollbar-width: none;
  -ms-overflow-style: none;
  display: flex;
  gap: 6px;
  overflow-x: auto;
  flex: 1;
  min-width: 0;
  padding-bottom: 3px;
}
.signal-tabs::-webkit-scrollbar {
  display: none;
}
.signal-tabs a {
  color: var(--color-text-muted, #8c9bad);
  white-space: nowrap;
  text-decoration: none;
  padding: 8px 15px;
  border: 1px solid transparent;
  border-radius: 7px;
  font-size: 13px;
}
.signal-tabs a:hover {
  color: var(--color-text, #e5edf5);
  background: var(--color-surface, #111d2b);
}
.signal-tabs a.active {
  color: #7dd3e8;
  background: rgba(75, 172, 198, 0.1);
  border-color: rgba(75, 172, 198, 0.24);
}
.signal-toolbar {
  display: flex;
  gap: 10px;
  align-items: center;
  flex-wrap: wrap;
  padding: 18px 20px 12px;
}
.search-input {
  width: 240px;
}
.board-select {
  width: 150px;
}
.signal-meta {
  display: flex;
  gap: 20px;
  flex-wrap: wrap;
  padding: 0 20px 16px;
}
.signal-meta strong {
  color: var(--color-text, #e5edf5);
  font-weight: 500;
}
.table-footer {
  padding: 18px 20px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 15px;
  flex-wrap: wrap;
}
.column-options {
  display: grid;
  grid-template-columns: repeat(2, minmax(100px, 1fr));
  gap: 10px;
  padding: 8px;
}
.panel :deep(.stock-link) {
  display: flex;
  flex-direction: column;
  gap: 4px;
  text-decoration: none;
  color: var(--color-text);
}
.panel :deep(.stock-link strong) {
  font-weight: 500;
}
.panel :deep(.stock-code) {
  font-size: 11px;
  color: var(--color-text-muted);
}
.section {
  margin-top: 18px;
}
@media (max-width: 640px) {
  .signal-toolbar {
    padding: 14px;
  }
  .search-input {
    width: calc(100% - 80px);
  }
  .board-select {
    width: 160px;
  }
  .signal-meta {
    padding: 0 14px 14px;
    gap: 10px;
  }
  .table-footer {
    padding: 14px;
  }
  .signal-navigation {
    margin-top: 18px;
  }
}
</style>
