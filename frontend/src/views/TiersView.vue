<script setup lang="ts">
import { computed, h, onBeforeUnmount, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import {
  NAlert,
  NButton,
  NButtonGroup,
  NDataTable,
  NSelect,
  NTag,
  type DataTableColumns,
  type DataTableSortState,
} from "naive-ui";
import { getJson } from "../api";
import PageState from "../components/PageState.vue";
import {
  change,
  localTime,
  message,
  number,
  numeric,
  percent,
  stockLink,
} from "../components/presentation";

interface ExecutionSide {
  date?: string;
  price?: number;
  shares?: number;
  cash?: number;
  cost?: number;
}
interface ExitResult {
  status: string;
  reason?: string;
  target_date?: string;
  sell?: ExecutionSide | null;
  net_return?: number | null;
  total_cost?: number | null;
  attempts?: { date: string; reason: string }[];
}
interface TierRow {
  date: string;
  tier: number;
  tier_label: string;
  secid: string;
  code: string;
  name: string;
  industry: string;
  reason: string;
  generated_at: string;
  status: string;
  source: string;
  entry_date?: string;
  entry?: number | null;
  rets: Record<string, number | null>;
  excess: Record<string, number | null>;
  max_gain?: number | null;
  max_drawdown?: number | null;
  execution: {
    status: string;
    reason: string;
    buy?: ExecutionSide | null;
    entry_attempts?: { date: string; reason: string }[];
    exits: Record<string, ExitResult>;
  };
}
interface TierStats {
  label: string;
  picks: number;
  finished: number;
  [key: string]: unknown;
}
interface ExecutionStats {
  n: number;
  total: number;
  avg_net_return: number | null;
  win_rate: number | null;
  avg_cost: number | null;
  states: Record<string, number>;
}
interface TierData {
  rows: TierRow[];
  horizons: number[];
  by_tier: Record<string, TierStats>;
  execution: {
    version: string;
    parameters: {
      capital: number;
      commission_bps: number;
      min_commission: number;
      transfer_bps: number;
      sell_tax_bps: number;
      slippage_bps: number;
    };
    by_tier: Record<string, Record<string, ExecutionStats>>;
  };
  publication?: {
    market_date?: string;
    summary_date?: string;
    generated_at?: string;
    reason?: string;
  };
  unmatched?: { date?: string; code?: string; name?: string }[];
}
const route = useRoute();
const router = useRouter();
const data = ref<TierData | null>(null);
const loading = ref(true);
const error = ref("");
const horizon = ref(5);
const tier = ref("all");
const mode = ref("execution");
const days = computed(() =>
  [30, 90, 180].includes(Number(route.query.days))
    ? Number(route.query.days)
    : 90,
);
let controller: AbortController | undefined;
const statusNames: Record<string, string> = {
  closed: "完成买卖",
  pending_entry: "待买入",
  entered: "持仓中",
  entry_expired: "买入窗口已过",
  pending_exit: "持仓待卖",
  blocked_exit: "卖出受阻",
  unavailable: "不可判定",
  excluded: "不纳入模拟",
};
const displayedRows = computed(() =>
  (data.value?.rows || []).filter(
    (row) => tier.value === "all" || String(row.tier) === tier.value,
  ),
);
const horizonOptions = computed(() =>
  (data.value?.horizons || [1, 5, 10, 20]).map((value) => ({
    label: `${value} 个交易日`,
    value,
  })),
);
const summaries = computed(() =>
  Object.entries(data.value?.execution?.by_tier || {})
    .filter(([key]) => tier.value === "all" || key === tier.value)
    .map(([key, windows]) => ({
      key,
      label: `第${key === "1" ? "一" : key === "2" ? "二" : key}梯队`,
      ...windows[String(horizon.value)],
    })),
);
const observationSummaries = computed(() =>
  Object.values(data.value?.by_tier || {}).filter(
    (row) => tier.value === "all" || String(row.tier) === tier.value,
  ),
);
const retrospectiveCount = computed(
  () =>
    data.value?.rows.filter((row) => row.status === "事后补生成").length || 0,
);
const exportUrl = computed(() => `/tiers.csv?days=${days.value}`);

function observationAverage(summary: TierStats): unknown {
  const statistics = summary[String(horizon.value)] as
    Record<string, unknown> | undefined;
  return statistics?.avg;
}

function result(row: TierRow): ExitResult {
  return (
    row.execution?.exits?.[String(horizon.value)] || {
      status: row.execution?.status || "unavailable",
      reason: row.execution?.reason,
    }
  );
}
function details(row: TierRow) {
  const outcome = result(row);
  const attempts = [
    ...(row.execution?.entry_attempts || []),
    ...(outcome.attempts || []),
  ];
  const reasonUrl = `/api/tier-reasons?${new URLSearchParams({ date: row.date, symbols: row.secid })}`;
  return h("div", { class: "order-details" }, [
    h("p", [
      h("span", { class: "data-note" }, "原始发布时间 "),
      localTime(row.generated_at),
    ]),
    h("p", [
      h("span", { class: "data-note" }, "当前状态 "),
      outcome.reason || statusNames[outcome.status] || outcome.status,
    ]),
    row.reason ? h("p", row.reason) : null,
    ...attempts.map((item) =>
      h(
        "p",
        { key: `${item.date}-${item.reason}` },
        `${item.date} · ${item.reason}`,
      ),
    ),
    row.source === "summary"
      ? h(
          "a",
          { href: reasonUrl, target: "_blank", rel: "noopener noreferrer" },
          "查看原文入池依据",
        )
      : h("span", { class: "data-note" }, "手工记录"),
  ]);
}
const sorting = ref<{ columnKey: string; order: "ascend" | "descend" | false }>(
  { columnKey: "date", order: "descend" },
);
function updateSorting(
  value: DataTableSortState | DataTableSortState[] | null,
) {
  const state = Array.isArray(value) ? value[0] : value;
  sorting.value = {
    columnKey: String(state?.columnKey || ""),
    order: state?.order || false,
  };
}
function sortable(
  key: string,
  read: (row: TierRow) => string | number | null | undefined,
) {
  return {
    sorter: (a: TierRow, b: TierRow) => {
      const left = read(a);
      const right = read(b);
      const emptyLeft = left === null || left === undefined || left === "";
      const emptyRight = right === null || right === undefined || right === "";
      if (emptyLeft && emptyRight) return 0;
      // Naive UI 会反转降序比较结果，缺失项在两个方向均放到末尾。
      const missing = sorting.value.order === "descend" ? -1 : 1;
      if (emptyLeft) return missing;
      if (emptyRight) return -missing;
      if (typeof left === "number" && typeof right === "number")
        return left - right;
      return String(left).localeCompare(String(right), "zh-CN");
    },
    sortOrder:
      sorting.value.columnKey === key ? sorting.value.order : (false as const),
  };
}
const commonColumns = computed<DataTableColumns<TierRow>>(() => [
  { type: "expand", width: 38, renderExpand: details },
  {
    title: "名单日",
    key: "date",
    width: 116,
    ...sortable("date", (row) => row.date),
  },
  {
    title: "梯队",
    key: "tier_label",
    width: 104,
    ...sortable("tier_label", (row) => row.tier),
  },
  {
    title: "股票",
    key: "stock",
    width: 160,
    render: stockLink,
    ...sortable("stock", (row) => row.name),
  },
  {
    title: "行业",
    key: "industry",
    width: 128,
    ellipsis: { tooltip: true },
    ...sortable("industry", (row) => row.industry),
  },
]);
const columns = computed<DataTableColumns<TierRow>>(() =>
  mode.value === "execution"
    ? [
        ...commonColumns.value,
        {
          title: "模拟买入",
          key: "buy",
          ...sortable("buy", (row) => numeric(row.execution?.buy?.price)),
          width: 132,
          align: "right",
          render: (row) =>
            h("div", { class: "trade-cell" }, [
              h(
                "span",
                { class: "tabular" },
                number(row.execution?.buy?.price),
              ),
              h("small", row.execution?.buy?.date || "—"),
            ]),
        },
        {
          title: "模拟卖出",
          key: "sell",
          ...sortable("sell", (row) => numeric(result(row).sell?.price)),
          width: 132,
          align: "right",
          render: (row) =>
            h("div", { class: "trade-cell" }, [
              h("span", { class: "tabular" }, number(result(row).sell?.price)),
              h("small", result(row).sell?.date || "—"),
            ]),
        },
        {
          title: `${horizon.value} 日净收益`,
          key: "net_return",
          ...sortable("net_return", (row) => numeric(result(row).net_return)),
          width: 130,
          align: "right",
          render: (row) => change(result(row).net_return),
        },
        {
          title: "合计成本 / 元",
          key: "cost",
          ...sortable("cost", (row) => numeric(result(row).total_cost)),
          width: 130,
          align: "right",
          render: (row) => number(result(row).total_cost),
        },
        {
          title: "状态",
          key: "state",
          ...sortable("state", (row) => result(row).status),
          width: 145,
          render: (row) => {
            const outcome = result(row);
            return h(
              NTag,
              {
                size: "small",
                bordered: false,
                type:
                  outcome.status === "closed"
                    ? "success"
                    : outcome.status === "excluded" ||
                        outcome.status === "unavailable"
                      ? "default"
                      : "warning",
              },
              { default: () => statusNames[outcome.status] || outcome.status },
            );
          },
        },
      ]
    : [
        ...commonColumns.value,
        {
          title: "参考入场价",
          key: "entry",
          ...sortable("entry", (row) => numeric(row.entry)),
          width: 120,
          align: "right",
          render: (row) => number(row.entry),
        },
        {
          title: `${horizon.value} 日价格变动`,
          key: "observation",
          ...sortable("observation", (row) =>
            numeric(row.rets[String(horizon.value)]),
          ),
          width: 145,
          align: "right",
          render: (row) => change(row.rets[String(horizon.value)]),
        },
        {
          title: "同期指数超额",
          key: "excess",
          ...sortable("excess", (row) =>
            numeric(row.excess[String(horizon.value)]),
          ),
          width: 135,
          align: "right",
          render: (row) => change(row.excess[String(horizon.value)]),
        },
        {
          title: "最高价相对入场",
          key: "max_gain",
          ...sortable("max_gain", (row) => numeric(row.max_gain)),
          width: 145,
          align: "right",
          render: (row) => change(row.max_gain),
        },
        {
          title: "最低价相对入场",
          key: "max_drawdown",
          ...sortable("max_drawdown", (row) => numeric(row.max_drawdown)),
          width: 145,
          align: "right",
          render: (row) => change(row.max_drawdown),
        },
        {
          title: "记录状态",
          key: "status",
          ...sortable("status", (row) => row.status),
          width: 130,
          render: (row) =>
            h(
              NTag,
              {
                size: "small",
                bordered: false,
                type: row.status === "事后补生成" ? "warning" : "default",
              },
              { default: () => row.status },
            ),
        },
      ],
);
const pagination = {
  pageSize: 20,
  showSizePicker: true,
  pageSizes: [20, 50, 100],
};

async function load() {
  controller?.abort();
  controller = new AbortController();
  const current = controller;
  loading.value = true;
  error.value = "";
  try {
    data.value = await getJson<TierData>(
      "/api/tiers",
      { days: days.value },
      current.signal,
    );
  } catch (e) {
    if (!current.signal.aborted) error.value = message(e);
  } finally {
    if (!current.signal.aborted) loading.value = false;
  }
}
function changeDays(value: number) {
  router.replace({ path: "/tiers", query: { days: String(value) } });
}
watch(days, load, { immediate: true });
watch(mode, () => {
  sorting.value = { columnKey: "date", order: "descend" };
});
onBeforeUnmount(() => controller?.abort());
</script>

<template>
  <div class="view-heading">
    <div>
      <h1 class="view-title">梯队跟踪</h1>
      <p class="view-caption">跟踪原始名单表现，核对 T+1 约束下的模拟买卖。</p>
    </div>
    <div class="tier-actions">
      <n-select
        :value="days"
        :options="
          [30, 90, 180].map((value) => ({
            label: `最近 ${value} 个名单日`,
            value,
          }))
        "
        style="width: 184px"
        @update:value="changeDays"
      /><n-button tag="a" :href="exportUrl" download secondary
        >下载 CSV</n-button
      >
    </div>
  </div>
  <page-state
    :loading="loading"
    :error="error"
    :empty="!data?.rows.length"
    title="暂无梯队记录"
    description="复盘名单发布后，可以在这里查看跟踪结果。"
    @retry="load"
  >
    <div class="tier-meta data-note">
      <span
        >行情截至
        <strong>{{ data?.publication?.market_date || "—" }}</strong></span
      ><span>最近名单 {{ data?.publication?.summary_date || "—" }}</span
      ><span>{{ data?.rows.length }} 条记录</span>
    </div>
    <n-alert
      v-if="retrospectiveCount"
      type="info"
      :show-icon="false"
      class="section"
      >{{
        retrospectiveCount
      }}
      条事后补生成记录已单独标记，并排除于实时样本统计。展开明细可查看发布时间。</n-alert
    >
    <n-alert
      v-if="data?.unmatched?.length"
      type="warning"
      :show-icon="false"
      class="section"
      >有 {{ data.unmatched.length }} 条名单内容未能匹配本地证券，完整记录见
      <a :href="`/api/tiers?days=${days}`" target="_blank">原始数据</a
      >。</n-alert
    >
    <section class="panel section">
      <div class="tier-toolbar">
        <n-button-group
          role="group"
          aria-label="收益口径"
          class="tier-mode-switch"
        >
          <n-button
            :type="mode === 'execution' ? 'primary' : 'default'"
            :aria-pressed="mode === 'execution'"
            @click="mode = 'execution'"
            >T+1 模拟净收益</n-button
          >
          <n-button
            :type="mode === 'observation' ? 'primary' : 'default'"
            :aria-pressed="mode === 'observation'"
            @click="mode = 'observation'"
            >价格观察</n-button
          >
        </n-button-group>
        <div class="tier-filters">
          <n-select
            v-model:value="tier"
            :options="[
              { label: '全部梯队', value: 'all' },
              { label: '第一梯队', value: '1' },
              { label: '第二梯队', value: '2' },
            ]"
            style="width: 130px"
          /><n-select
            v-model:value="horizon"
            :options="horizonOptions"
            style="width: 145px"
          />
        </div>
      </div>
      <div v-if="mode === 'execution'" class="tier-summary-grid">
        <div
          v-for="summary in summaries"
          :key="summary.key"
          class="tier-summary"
        >
          <div class="summary-label">
            {{ summary.label }} <span class="data-note">{{ horizon }} 日</span>
          </div>
          <div class="summary-return">
            <span
              :class="
                summary.avg_net_return == null
                  ? ''
                  : summary.avg_net_return > 0
                    ? 'up'
                    : summary.avg_net_return < 0
                      ? 'down'
                      : ''
              "
              >{{ percent(summary.avg_net_return) }}</span
            ><small class="data-note">平均净收益</small>
          </div>
          <div class="summary-statistics">
            <span>完成 {{ summary.n || 0 }} / {{ summary.total || 0 }}</span
            ><span>胜率 {{ percent(summary.win_rate, true, 1) }}</span
            ><span>平均成本 {{ number(summary.avg_cost) }} 元</span>
          </div>
          <div class="summary-states data-note">
            {{
              Object.entries(summary.states || {})
                .map(([key, count]) => `${statusNames[key] || key} ${count}`)
                .join(" · ")
            }}
          </div>
        </div>
      </div>
      <div v-else class="tier-summary-grid">
        <div
          v-for="summary in observationSummaries"
          :key="summary.label"
          class="tier-summary"
        >
          <div class="summary-label">
            {{ summary.label }}
            <span class="data-note">{{ horizon }} 日价格观察</span>
          </div>
          <div class="summary-return">
            <span>{{ percent(observationAverage(summary)) }}</span
            ><small class="data-note">平均价格变动</small>
          </div>
          <p class="data-note">参考价格统计未计交易费用和成交约束。</p>
        </div>
      </div>
      <n-data-table
        :columns="columns"
        :data="displayedRows"
        :row-key="(row) => `${row.date}-${row.tier}-${row.secid}`"
        :scroll-x="mode === 'execution' ? 1350 : 1450"
        :pagination="pagination"
        @update:sorter="updateSorting"
        :bordered="false"
        size="small"
      />
    </section>
    <p
      v-if="mode === 'execution' && data?.execution?.parameters"
      class="data-note section"
    >
      按日线开盘与收盘模拟，每笔预算
      {{ number(data.execution.parameters.capital / 10000, 0) }}
      万元。最早买入后的下一交易日卖出。双边佣金
      {{ data.execution.parameters.commission_bps }} bp，最低
      {{ data.execution.parameters.min_commission }} 元；滑点每边
      {{ data.execution.parameters.slippage_bps }}
      bp。停牌、涨跌停及价格基础缺口保留状态。
    </p>
    <p v-else class="data-note section">
      最高价和最低价用于观察价格区间，未假定在极值点成交。事后补榜的价格收益留空。
    </p>
  </page-state>
</template>

<style scoped>
.tier-actions {
  display: flex;
  gap: 10px;
}
.section {
  margin-top: 20px;
}
.tier-meta {
  display: flex;
  gap: 22px;
  flex-wrap: wrap;
  margin-top: 22px;
}
.tier-meta strong {
  font-weight: 500;
  color: var(--color-text);
}
.tier-toolbar {
  padding: 18px 22px 0;
  display: flex;
  gap: 20px;
  align-items: center;
  justify-content: space-between;
}
.tier-mode-switch {
  width: auto;
  min-width: 280px;
}
.tier-filters {
  display: flex;
  gap: 10px;
}
.tier-summary-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  padding: 24px 22px;
  gap: 30px;
  border-bottom: 1px solid var(--color-border);
}
.tier-summary + .tier-summary {
  border-left: 1px solid var(--color-border);
  padding-left: 30px;
}
.summary-label {
  display: flex;
  gap: 12px;
  align-items: center;
  font-size: 13px;
  font-weight: 500;
}
.summary-return {
  display: flex;
  align-items: baseline;
  gap: 13px;
  margin: 12px 0;
}
.summary-return > span {
  font-size: 31px;
  line-height: 1.3;
  font-weight: 500;
  font-variant-numeric: tabular-nums;
  letter-spacing: -0.5px;
}
.summary-statistics {
  display: flex;
  gap: 16px;
  font-size: 12px;
  color: var(--color-text-muted);
  flex-wrap: wrap;
}
.summary-states {
  margin-top: 12px;
  line-height: 1.7;
  font-size: 11px;
}
.panel :deep(.trade-cell) {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.panel :deep(.trade-cell small) {
  font-size: 10px;
  color: var(--color-text-muted);
}
.panel :deep(.order-details) {
  padding: 7px 20px 15px;
  font-size: 12px;
  line-height: 1.7;
  color: var(--color-text-muted);
}
.panel :deep(.order-details a) {
  color: var(--color-accent);
}
.panel :deep(.order-details p) {
  margin: 6px 0;
}
.panel :deep(.stock-link) {
  text-decoration: none;
  display: flex;
  flex-direction: column;
  gap: 4px;
  color: var(--color-text);
}
.panel :deep(.stock-link strong) {
  font-weight: 500;
}
.panel :deep(.stock-code) {
  color: var(--color-text-muted);
  font-size: 11px;
}
@media (max-width: 850px) {
  .tier-toolbar {
    flex-wrap: wrap;
    padding: 10px 17px 17px;
    gap: 10px;
  }
  .tier-summary-grid {
    padding: 20px 17px;
    gap: 20px;
  }
  .tier-summary + .tier-summary {
    padding-left: 20px;
  }
}
@media (max-width: 560px) {
  .tier-actions {
    width: 100%;
  }
  .tier-summary-grid {
    grid-template-columns: 1fr;
  }
  .tier-summary + .tier-summary {
    border-left: 0;
    border-top: 1px solid var(--color-border);
    padding: 20px 0 0;
  }
  .summary-return > span {
    font-size: 27px;
  }
  .tier-meta {
    gap: 10px;
  }
  .tier-mode-switch {
    min-width: 100%;
    width: 100%;
  }
}
</style>
