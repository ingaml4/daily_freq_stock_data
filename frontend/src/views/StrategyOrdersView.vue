<script setup lang="ts">
import { computed, onBeforeUnmount, ref, shallowRef, watch } from "vue";
import { useRoute } from "vue-router";
import { NButton, NInput, NPagination, NTag } from "naive-ui";
import { ApiError, getJson } from "../api";
import PageState from "../components/PageState.vue";
import StrategyOrderDetail from "../components/StrategyOrderDetail.vue";
import { message, numeric, percent } from "../components/presentation";
import {
  label,
  normalizeCandidates,
  normalizeReview,
  orderMode,
  policies,
  reviewModes,
  statuses,
  statusTone,
  type Candidate,
  type OrderRow,
  type ReviewOrders,
} from "../strategyOrders";

const datasets = [
  {
    id: "original",
    name: "原策略比较",
    api: "/api/strategy-review/orders",
    filename: "strategy-review-orders.json",
    description: "按方案查看各次模拟买卖及其信号依据。",
  },
  {
    id: "abc",
    name: "A/B/C 入场时点实验",
    api: "/api/strategy-review/entry-timing/orders",
    filename: "abc-entry-timing-orders.json",
    description: "同一候选并列展示两种入场规则。列表使用 D+5 固定退出，详情也可查看结构退出。",
  },
] as const;
const route = useRoute();
const dataset = computed(() => route.query.dataset === "abc" ? datasets[1] : datasets[0]);
const isAbc = computed(() => dataset.value.id === "abc");
const jsonUrl = computed(() => `${dataset.value.api}?format=json`);
const rows = shallowRef<OrderRow[]>([]);
const loading = ref(true);
const error = ref("");
let controller: AbortController | undefined;

const from = ref("");
const through = ref("");
const security = ref("");
const mode = ref<string | null>(null);
const status = ref<string | null>(null);
const sort = ref("recent");
const page = ref(1);
const pageSize = ref(20);
const selected = shallowRef<OrderRow | null>(null);
const detailOpen = ref(false);

const modes = computed(() => [...new Set(rows.value.map((row) => row.mode))]
  .sort()
  .map((value) => ({
    label: isAbc.value ? `模式 ${value}` : reviewModes[value] || value,
    value,
  })));
const statusOptions = computed(() => {
  const recorded = new Set(rows.value.flatMap((row) =>
    row.dataset === "original" ? [row.raw.status] : Object.values(row.raw.orders).map((order) => order.status),
  ));
  return [...new Set([...Object.keys(statuses), ...recorded])]
    .filter((value) => recorded.has(value))
    .map((value) => ({ label: label(value), value }));
});
const sortOptions = computed(() => [
  { label: "信号日从新到旧", value: "recent" },
  { label: "信号日从旧到新", value: "oldest" },
  { label: "证券代码升序", value: "code" },
  ...(isAbc.value
    ? [
        { label: "开盘组净收益降序", value: "open-high" },
        { label: "开盘组净收益升序", value: "open-low" },
        { label: "确认组净收益降序", value: "confirm-high" },
        { label: "确认组净收益升序", value: "confirm-low" },
      ]
    : [
        { label: "净收益降序", value: "open-high" },
        { label: "净收益升序", value: "open-low" },
      ]),
]);
const dateRange = computed(() => {
  const dates = rows.value.map((row) => row.date).sort();
  return dates.length ? `${dates[0]} 至 ${dates.at(-1)}` : "";
});
const filtersActive = computed(() => Boolean(from.value || through.value || security.value || mode.value || status.value));
const invalidRange = computed(() => Boolean(from.value && through.value && from.value > through.value));
const filtered = computed(() => {
  if (invalidRange.value) return [];
  const query = security.value.trim().toLowerCase();
  return rows.value.filter((row) => {
    if ((from.value && row.date < from.value) || (through.value && row.date > through.value)) return false;
    if (mode.value && row.mode !== mode.value) return false;
    if (status.value) {
      const matches = row.dataset === "original"
        ? row.raw.status === status.value
        : Object.values(row.raw.orders).some((order) => order.status === status.value);
      if (!matches) return false;
    }
    if (!query) return true;
    const fields = [row.name, row.secid, symbol(row)];
    return fields.some((field) => field?.toLowerCase().includes(query));
  }).sort(compare);
});
const visible = computed(() => {
  const offset = (page.value - 1) * pageSize.value;
  return filtered.value.slice(offset, offset + pageSize.value);
});

function code(row: OrderRow) {
  return row.secid.split(".").at(-1) || row.secid;
}

function symbol(row: OrderRow) {
  if (row.dataset === "abc" && row.raw.symbol) return row.raw.symbol;
  const prefix = row.secid.startsWith("1.") ? "SH" : row.secid.startsWith("2.") ? "BJ" : "SZ";
  return `${prefix}${code(row)}`;
}

function net(row: OrderRow, policy: "O" | "R") {
  return numeric(row.dataset === "original" ? row.raw.net_return : row.raw.orders[policy]?.net_return);
}

function netClass(value: number | null) {
  if (value === null || value === 0) return "";
  return value > 0 ? "up" : "down";
}

function compare(left: OrderRow, right: OrderRow) {
  const date = right.date.localeCompare(left.date);
  const security = code(left).localeCompare(code(right));
  if (sort.value === "recent") return date || security;
  if (sort.value === "oldest") return -date || security;
  if (sort.value === "code") return security || date;
  const policy = sort.value.startsWith("confirm") ? "R" : "O";
  const l = net(left, policy);
  const r = net(right, policy);
  if (l === null && r !== null) return 1;
  if (r === null && l !== null) return -1;
  const difference = (l ?? 0) - (r ?? 0);
  return (sort.value.endsWith("high") ? -difference : difference) || date || security;
}

function clearFilters() {
  from.value = "";
  through.value = "";
  security.value = "";
  mode.value = null;
  status.value = null;
}

function showDetail(row: OrderRow) {
  selected.value = row;
  detailOpen.value = true;
}

async function load() {
  controller?.abort();
  const current = new AbortController();
  controller = current;
  const active = dataset.value;
  loading.value = true;
  error.value = "";
  rows.value = [];
  try {
    const data = active.id === "abc"
      ? normalizeCandidates(await getJson<Candidate[]>(active.api, undefined, current.signal))
      : normalizeReview(await getJson<ReviewOrders>(active.api, undefined, current.signal));
    if (!current.signal.aborted) rows.value = data;
  } catch (e) {
    if (!current.signal.aborted && !(e instanceof ApiError && e.status === 404)) {
      error.value = message(e);
    }
  } finally {
    if (!current.signal.aborted) loading.value = false;
  }
}

watch([from, through, security, mode, status, sort, pageSize], () => { page.value = 1; });
watch(dataset, () => {
  clearFilters();
  sort.value = "recent";
  page.value = 1;
  detailOpen.value = false;
  void load();
}, { immediate: true });
onBeforeUnmount(() => controller?.abort());
</script>

<template>
  <router-link to="/strategy-review" class="back-link">← 返回策略验证</router-link>
  <div class="view-heading">
    <div>
      <h1 class="view-title">逐笔回放</h1>
      <p class="view-caption">{{ dataset.description }}</p>
    </div>
    <div class="json-actions" role="group" aria-label="原始逐笔数据">
      <n-button tag="a" :href="jsonUrl" target="_blank" rel="noopener" secondary size="small">
        查看 JSON
      </n-button>
      <n-button tag="a" :href="jsonUrl" :download="dataset.filename" secondary size="small">
        下载 JSON
      </n-button>
    </div>
  </div>

  <nav class="dataset-tabs" aria-label="逐笔数据集">
    <router-link
      v-for="option in datasets"
      :key="option.id"
      :to="{ path: '/strategy-review/orders', query: { dataset: option.id } }"
      :class="{ active: option.id === dataset.id }"
      :aria-current-value="option.id === dataset.id ? 'page' : 'false'"
    >
      {{ option.name }}
    </router-link>
  </nav>

  <section class="panel orders-panel" :aria-label="dataset.name">
    <div class="filters" role="group" aria-label="逐笔筛选">
      <label class="filter">
        <span>证券</span>
        <n-input v-model:value="security" clearable placeholder="名称、代码或交易所代码" />
      </label>
      <label class="filter">
        <span>起始信号日</span>
        <input v-model="from" type="date" class="filter-control tabular" />
      </label>
      <label class="filter">
        <span>结束信号日</span>
        <input v-model="through" type="date" class="filter-control tabular" />
      </label>
      <label class="filter">
        <span id="orders-mode-label">{{ isAbc ? "模式" : "方案" }}</span>
        <select v-model="mode" class="filter-control" aria-labelledby="orders-mode-label">
          <option :value="null">全部</option>
          <option v-for="option in modes" :key="option.value" :value="option.value">{{ option.label }}</option>
        </select>
      </label>
      <label class="filter">
        <span id="orders-status-label">{{ isAbc ? "任一规则状态" : "成交状态" }}</span>
        <select v-model="status" class="filter-control" aria-labelledby="orders-status-label">
          <option :value="null">全部</option>
          <option v-for="option in statusOptions" :key="option.value" :value="option.value">{{ option.label }}</option>
        </select>
      </label>
      <label class="filter">
        <span id="orders-sort-label">排序</span>
        <select v-model="sort" class="filter-control" aria-labelledby="orders-sort-label">
          <option v-for="option in sortOptions" :key="option.value" :value="option.value">{{ option.label }}</option>
        </select>
      </label>
    </div>
    <div v-if="!loading && rows.length" class="orders-summary">
      <p class="data-note" aria-live="polite">
        <strong class="tabular">{{ filtered.length.toLocaleString("zh-CN") }}</strong>
        / {{ rows.length.toLocaleString("zh-CN") }} {{ isAbc ? "条候选" : "条订单" }}
        <span class="orders-period tabular">{{ dateRange }}</span>
      </p>
      <n-button v-if="filtersActive" text size="small" @click="clearFilters">清除筛选</n-button>
    </div>

    <page-state
      :loading="loading"
      :error="error"
      :empty="!filtered.length"
      :title="invalidRange ? '日期范围无效' : rows.length ? '没有符合条件的记录' : '暂无逐笔数据'"
      :description="invalidRange ? '起始信号日应早于或等于结束信号日。' : rows.length ? '调整筛选条件后再查看。' : '生成逐笔回放后，记录会显示在这里。'"
      @retry="load"
    >
      <table class="orders-table tabular" :aria-label="dataset.name + '逐笔记录'" role="table">
        <thead role="rowgroup">
          <tr role="row">
            <th scope="col" role="columnheader">证券</th>
            <th scope="col" role="columnheader">信号日</th>
            <th scope="col" role="columnheader">{{ isAbc ? "模式" : "方案" }}</th>
            <th scope="col" role="columnheader">{{ isAbc ? policies[0].name : "成交状态" }}</th>
            <th scope="col" role="columnheader">{{ isAbc ? policies[1].name : "净收益" }}</th>
            <th scope="col" role="columnheader">详情</th>
          </tr>
        </thead>
        <tbody role="rowgroup">
          <tr v-for="row in visible" :key="row.id" role="row">
            <td class="security-cell" role="cell">
              <strong>{{ row.name || code(row) }}</strong>
              <span class="data-note">{{ symbol(row) }}</span>
            </td>
            <td class="date-cell" role="cell">
              <time :datetime="row.date">{{ row.date }}</time>
            </td>
            <td class="mode-cell" role="cell">{{ orderMode(row) }}</td>
            <template v-if="row.dataset === 'abc'">
              <td v-for="policy in policies" :key="policy.id" class="outcome-cell" role="cell">
                <span class="mobile-label">{{ policy.name }}</span>
                <span v-if="row.raw.orders[policy.id]" class="outcome">
                  <n-tag size="small" :bordered="false" :type="statusTone(row.raw.orders[policy.id]!.status)">
                    {{ label(row.raw.orders[policy.id]!.status) }}
                  </n-tag>
                  <span class="outcome-return" :class="netClass(net(row, policy.id))">{{ percent(net(row, policy.id)) }}</span>
                </span>
                <span v-else class="data-note">—</span>
              </td>
            </template>
            <template v-else>
              <td class="outcome-cell" role="cell">
                <span class="mobile-label">成交状态</span>
                <n-tag size="small" :bordered="false" :type="statusTone(row.raw.status)">
                  {{ label(row.raw.status) }}
                </n-tag>
              </td>
              <td class="outcome-cell" role="cell">
                <span class="mobile-label">净收益</span>
                <span class="outcome-return" :class="netClass(net(row, 'O'))">{{ percent(net(row, 'O')) }}</span>
              </td>
            </template>
            <td class="detail-cell" role="cell">
              <n-button
                text
                type="primary"
                size="small"
                :aria-label="`查看 ${row.date} ${row.name || code(row)} ${orderMode(row)} 详情`"
                @click="showDetail(row)"
              >查看<span aria-hidden="true"> ↗</span></n-button>
            </td>
          </tr>
        </tbody>
      </table>
      <div class="orders-footer">
        <label class="page-size">
          <span id="orders-page-size-label" class="data-note">每页</span>
          <select v-model.number="pageSize" class="filter-control tabular" aria-labelledby="orders-page-size-label">
            <option v-for="size in [20, 50, 100]" :key="size" :value="size">{{ size }}</option>
          </select>
        </label>
        <n-pagination v-model:page="page" :page-size="pageSize" :item-count="filtered.length" :page-slot="5" />
      </div>
    </page-state>
  </section>
  <strategy-order-detail v-model:show="detailOpen" :row="selected" />
</template>

<style scoped>
.back-link {
  display: inline-block;
  margin-bottom: 12px;
  font-size: 13px;
}
.json-actions, .dataset-tabs {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
.dataset-tabs {
  margin-bottom: 14px;
}
.dataset-tabs a {
  padding: 7px 13px;
  border: 1px solid transparent;
  border-radius: 7px;
  color: var(--color-text-muted);
  font-size: 13px;
}
.dataset-tabs a:hover, .dataset-tabs a.active {
  background: var(--color-surface-raised);
  color: var(--color-accent);
}
.dataset-tabs a.active {
  border-color: #2e5760;
}
.orders-panel {
  padding: 0;
}
.filters {
  display: grid;
  grid-template-columns: minmax(150px, 1.3fr) repeat(5, minmax(110px, 1fr));
  gap: 10px;
  padding: 16px 18px;
}
.filter {
  display: grid;
  gap: 5px;
  min-width: 0;
  font-size: 12px;
  color: var(--color-text-muted);
}
.filter-control {
  width: 100%;
  min-width: 0;
  height: 34px;
  padding: 0 8px;
  border: 1px solid var(--color-border);
  border-radius: 8px;
  color: var(--color-text);
  color-scheme: dark;
  background: #ffffff0f;
  font-size: 13px;
}
.filter-control:hover, .filter-control:focus {
  border-color: var(--color-accent);
}
.filter-control:focus-visible {
  outline: 1px solid var(--color-accent);
}
.orders-summary, .orders-footer {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  justify-content: space-between;
  gap: 8px 16px;
  padding: 11px 18px;
}
.orders-summary {
  padding-top: 0;
}
.orders-summary p {
  margin: 0;
}
.orders-summary strong {
  color: var(--color-text);
  font-weight: 600;
}
.orders-period {
  display: inline-block;
  margin-left: 10px;
}
.orders-table {
  width: 100%;
  border-collapse: collapse;
  table-layout: fixed;
  text-align: left;
  font-size: 13px;
}
.orders-table th, .orders-table td {
  padding: 9px 12px;
  border-top: 1px solid var(--color-border);
}
.orders-table th {
  background: var(--color-surface-raised);
  font-weight: 500;
  color: #c3d2df;
}
.orders-table th:first-child, .orders-table td:first-child {
  padding-left: 18px;
}
.orders-table th:nth-child(2) {
  width: 15%;
}
.orders-table th:nth-child(4), .orders-table th:nth-child(5) {
  width: 18%;
}
.orders-table th:last-child {
  width: 9%;
}
.orders-table tbody tr:hover {
  background: #18273880;
}
.security-cell strong {
  display: block;
  font-weight: 600;
}
.security-cell .data-note {
  display: block;
  font-size: 11px;
}
.mode-cell {
  color: #c3d2df;
}
.outcome {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 4px 8px;
}
.outcome-return {
  font-weight: 600;
}
.mobile-label {
  display: none;
}
.orders-footer {
  border-top: 1px solid var(--color-border);
}
.page-size {
  display: flex;
  align-items: center;
  gap: 7px;
}
.page-size select {
  width: 66px;
}
@media (max-width: 1050px) {
  .filters {
    grid-template-columns: repeat(3, minmax(0, 1fr));
  }
}
@media (max-width: 850px) {
  .orders-table, .orders-table tbody {
    display: block;
  }
  .orders-table thead {
    position: absolute;
    width: 1px;
    height: 1px;
    overflow: hidden;
    clip-path: inset(50%);
    white-space: nowrap;
  }
  .orders-table tbody tr {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    align-items: start;
    gap: 7px 10px;
    padding: 13px 18px;
    border-top: 1px solid var(--color-border);
  }
  .orders-table tbody td, .orders-table tbody td:first-child {
    display: block;
    min-width: 0;
    padding: 0;
    border: 0;
  }
  .orders-table .date-cell, .orders-table .detail-cell {
    text-align: right;
  }
  .orders-table .mode-cell {
    grid-column: 1 / -1;
  }
  .orders-table .detail-cell {
    grid-column: 2;
  }
  .orders-table .detail-cell .n-button {
    min-height: 28px;
  }
  .mobile-label {
    display: block;
    margin-bottom: 3px;
    color: var(--color-text-muted);
    font-size: 11px;
  }
}
@media (max-width: 570px) {
  .filters {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
  .orders-period {
    display: block;
    margin-left: 0;
  }
  .filters, .orders-table tbody tr, .orders-summary, .orders-footer {
    padding-inline: 13px;
  }
}
</style>
