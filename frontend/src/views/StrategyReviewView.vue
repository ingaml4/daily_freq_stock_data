<script setup lang="ts">
import { computed, h, onBeforeUnmount, onMounted, ref } from "vue";
import {
  NAlert,
  NButton,
  NCollapse,
  NCollapseItem,
  NDataTable,
  NTag,
  type DataTableColumns,
} from "naive-ui";
import { ApiError, getJson } from "../api";
import PageState from "../components/PageState.vue";
import {
  change,
  localTime,
  message,
  percent,
} from "../components/presentation";

interface Statistics {
  selected: number;
  closed: number;
  signal_dates_with_closed_orders: number;
  execution_coverage: number | null;
  mean_net_return: number | null;
  win_rate: number | null;
}
interface Portfolio {
  valid_for_comparison: boolean;
  net_return: number | null;
  max_drawdown: number | null;
  valuation_complete: boolean;
}
interface Arm {
  out_of_sample: Statistics;
  out_of_sample_portfolio: Portfolio;
  promotion?: { approved: boolean; reasons: string[] };
}
interface ReviewData {
  asof: string;
  generated_at: string;
  version: string;
  approved_strategies: string[];
  coverage: {
    start: string;
    end: string;
    test_start: string;
    test_end: string;
    test_sessions: number;
    signal_sessions: number;
    market_sessions: number;
  };
  arms: Record<string, Arm>;
  policy: {
    minimum_test_dates: number;
    minimum_closed_orders: number;
    minimum_execution_coverage: number;
    primary_horizon: number;
    bootstrap_repetitions: number;
    bootstrap_block_sessions: number;
  };
  provenance: {
    point_in_time_universe: boolean;
    point_in_time_industries: boolean;
    comparable_indicator_price_history: boolean;
  };
}
type ArmRow = Arm & { key: string; name: string };
interface EntryTiming {
  start: string;
  mature_signal_end: string | null;
  mature_signal_sessions: number;
  mature_candidates: {
    candidates: number;
    O: Statistics;
    R: Statistics;
    paired: { both_closed: number; R_minus_O_mean: number | null; loss_skipped?: number; gain_missed?: number };
  };
  by_mode: Record<string, { candidates: number }>;
  cash_mature: { arms: Record<"O" | "R", Portfolio> };
}
const entryTiming = ref<EntryTiming | null>(null);
const entryArms = [
  { id: "O", label: "开盘直买" },
  { id: "R", label: "日线确认后买入" },
] as const;
const names: Record<string, string> = {
  baseline: "原四信号对照",
  setups: "趋势突破 + 止跌确认",
  setups_sector: "再加相对强弱排序",
  setups_sector_exit: "再加结构与时间退出",
};
const reasons: Record<string, string> = {
  historical_universe_and_status_unverified:
    "历史股票池及风险警示状态缺少可核验快照",
  historical_industry_membership_unverified: "历史行业归属缺少快照",
  indicator_price_basis_unverified: "指标形成窗口的价格基础未核验",
  insufficient_out_of_sample_dates: "有完成成交的样本外信号日不足",
  insufficient_closed_orders: "完成买卖数量不足",
  insufficient_execution_coverage: "可核验执行覆盖不足",
  invalid_execution_coverage: "执行覆盖数据无效",
  net_improvement_not_established: "尚未证实扣费后收益改善",
  baseline_evidence_insufficient: "对照组的可验证证据不足",
  portfolio_drawdown_validation_pending:
    "组合估值或执行数据不完整，回撤无法验证",
  portfolio_drawdown_deteriorated: "组合回撤恶化超出验收范围",
  portfolio_net_return_not_improved: "组合净收益未优于对照",
  double_slippage_stress_not_passed: "双倍滑点压力测试未通过",
};
const data = ref<ReviewData | null>(null);
const loading = ref(true);
const error = ref("");
let controller: AbortController | undefined;
const rows = computed<ArmRow[]>(() =>
  Object.entries(data.value?.arms || {}).map(([key, arm]) => ({
    ...arm,
    key,
    name: names[key] || key,
  })),
);
const approved = computed(() => !!data.value?.approved_strategies.length);
const columns: DataTableColumns<ArmRow> = [
  {
    title: "方案",
    key: "name",
    width: 225,
    fixed: "left",
    render: (row) =>
      h("div", { class: "strategy-name" }, [
        h("strong", row.name),
        row.key === "baseline"
          ? h("span", { class: "data-note" }, "四项原始信号的规则并集")
          : null,
      ]),
  },
  {
    title: "入选次数",
    key: "selected",
    align: "right",
    width: 100,
    render: (r) => r.out_of_sample.selected,
  },
  {
    title: "完成买卖",
    key: "closed",
    align: "right",
    width: 105,
    render: (r) => r.out_of_sample.closed,
  },
  {
    title: "可核验覆盖",
    key: "coverage",
    align: "right",
    width: 115,
    render: (r) => percent(r.out_of_sample.execution_coverage, true, 1),
  },
  {
    title: "单笔平均净收益",
    key: "return",
    align: "right",
    width: 150,
    render: (r) => change(r.out_of_sample.mean_net_return),
  },
  {
    title: "组合净收益",
    key: "portfolio_return",
    align: "right",
    width: 125,
    render: (r) =>
      r.out_of_sample_portfolio.valid_for_comparison
        ? change(r.out_of_sample_portfolio.net_return)
        : h("span", { class: "data-note" }, "无法验证"),
  },
  {
    title: "组合最大回撤",
    key: "drawdown",
    align: "right",
    width: 130,
    render: (r) =>
      r.out_of_sample_portfolio.valid_for_comparison
        ? change(r.out_of_sample_portfolio.max_drawdown)
        : h("span", { class: "data-note" }, "无法验证"),
  },
  {
    title: "验收",
    key: "approved",
    width: 132,
    render: (r) =>
      h(
        NTag,
        {
          size: "small",
          bordered: false,
          type:
            r.key === "baseline"
              ? "default"
              : r.promotion?.approved
                ? "success"
                : "warning",
        },
        {
          default: () =>
            r.key === "baseline"
              ? "对照组"
              : r.promotion?.approved
                ? "通过验收"
                : "未通过，未启用",
        },
      ),
  },
];
const uniqueReasons = computed(() => [
  ...new Set(rows.value.flatMap((r) => r.promotion?.reasons || [])),
]);
async function load() {
  controller?.abort();
  controller = new AbortController();
  const current = controller;
  loading.value = true;
  error.value = "";
  void getJson<EntryTiming>("/api/strategy-review/entry-timing", undefined, current.signal)
    .then((report) => { if (!current.signal.aborted) entryTiming.value = report; })
    .catch(() => { if (!current.signal.aborted) entryTiming.value = null; });
  try {
    data.value = await getJson<ReviewData>(
      "/api/strategy-review",
      undefined,
      current.signal,
    );
  } catch (e) {
    if (!current.signal.aborted) {
      if (e instanceof ApiError && e.status === 404) data.value = null;
      else error.value = message(e);
    }
  } finally {
    if (!current.signal.aborted) loading.value = false;
  }
}
onMounted(load);
onBeforeUnmount(() => controller?.abort());
</script>

<template>
  <div class="view-heading">
    <div>
      <h1 class="view-title">策略验证</h1>
      <p class="view-caption">用同口径回放检查新规则是否带来可以确认的改善。</p>
    </div>
    <n-button secondary :loading="loading" @click="load">刷新报告</n-button>
  </div>
  <page-state
    :loading="loading"
    :error="error"
    :empty="!data"
    title="尚无策略比较报告"
    description="完成离线回测后，结果会显示在这里。"
    @retry="load"
  >
    <n-alert
      :type="approved ? 'success' : 'warning'"
      :show-icon="false"
      class="section review-status"
      ><strong>{{
        approved ? "已有方案通过验收" : "本轮没有批准新规则进入正式选股"
      }}</strong>
      <p>
        {{
          approved
            ? "请结合各方案的具体验收记录查看。"
            : "当前数据未能证实整体改善。比较结果保留供复核，正式选股继续使用原规则。"
        }}
      </p></n-alert
    >
    <div class="review-window data-note">
      <span
        >数据截至 <strong>{{ data?.asof }}</strong></span
      ><span
        >样本外 {{ data?.coverage.test_start }} —
        {{ data?.coverage.test_end }}</span
      ><span
        ><strong>{{ data?.coverage.test_sessions }}</strong> 个信号日</span
      >
    </div>
    <section class="panel section">
      <div class="panel-heading">
        <div>
          <h2 class="section-heading">逐层比较</h2>
          <p class="data-note">
            相同费用和滑点 · 次日开盘尝试入场 ·
            {{ data?.policy.primary_horizon }} 个交易日主持有期
          </p>
        </div>
        <n-button
          tag="a"
          href="/api/strategy-review"
          target="_blank"
          secondary
          size="small"
          >完整报告</n-button
        >
      </div>
      <n-data-table
        :columns="columns"
        :data="rows"
        :row-key="(row) => row.key"
        :scroll-x="1182"
        :bordered="false"
        size="small"
      />
      <p class="table-note data-note">
        单笔平均净收益只统计可核验完成买卖的条件性样本，各组覆盖不同。执行或估值数据不完整的组合显示“无法验证”。
      </p>
    </section>
    <section v-if="entryTiming" class="panel detail-panel section">
      <h2 class="section-heading">A/B/C 入场时点实验</h2>
      <p class="data-note">
        {{ entryTiming.start }} 至 {{ entryTiming.mature_signal_end || "尚无到期信号日" }}，
        {{ entryTiming.mature_signal_sessions }} 个信号日已到 D+5，
        {{ entryTiming.mature_candidates.candidates }} 条候选。
        A {{ entryTiming.by_mode.A.candidates }} · B {{ entryTiming.by_mode.B.candidates }} · C {{ entryTiming.by_mode.C.candidates }}。
      </p>
      <p class="data-note">探索性日线回放。两组分别在 D+1 开盘尝试，或等 D+1 收盘确认后在 D+2 开盘尝试。盘中确认样本覆盖不足。</p>
      <div class="entry-table-wrap">
        <table class="entry-table">
          <thead><tr><th scope="col">入场规则</th><th scope="col">完成买卖</th><th scope="col">单笔平均净收益</th><th scope="col">组合净值变动</th><th scope="col">最大回撤</th></tr></thead>
          <tbody>
            <tr v-for="arm in entryArms" :key="arm.id">
              <th scope="row">{{ arm.label }}</th>
              <td>{{ entryTiming.mature_candidates[arm.id].closed }}</td>
              <td>{{ percent(entryTiming.mature_candidates[arm.id].mean_net_return, true, 2) }}</td>
              <td>{{ percent(entryTiming.cash_mature.arms[arm.id].net_return, true, 2) }}</td>
              <td>{{ percent(entryTiming.cash_mature.arms[arm.id].max_drawdown, true, 2) }}</td>
            </tr>
          </tbody>
        </table>
      </div>
      <p v-if="entryArms.some(arm => !entryTiming?.cash_mature.arms[arm.id].valid_for_comparison)" class="data-note">
        部分执行或估值有缺口。表内组合净值与回撤为条件性估算，持仓缺口仍占用资金。
      </p>
      <p class="data-note">
        两组均完成 {{ entryTiming.mature_candidates.paired.both_closed }} 笔，
        确认组相对开盘组平均净收益差 {{ entryTiming.mature_candidates.paired.R_minus_O_mean === null
          ? "—" : `${(entryTiming.mature_candidates.paired.R_minus_O_mean * 100).toFixed(2)} 个百分点` }}。
        确认组跳过 {{ entryTiming.mature_candidates.paired.loss_skipped ?? 0 }} 笔亏损，错过 {{ entryTiming.mature_candidates.paired.gain_missed ?? 0 }} 笔盈利。
      </p>
      <a href="/api/strategy-review/entry-timing">报告数据</a> ·
      <router-link :to="{ path: '/strategy-review/orders', query: { dataset: 'abc' } }"
        >逐笔对照</router-link
      >
    </section>
    <div class="review-details section">
      <section class="panel detail-panel">
        <h2 class="section-heading">本轮依据</h2>
        <dl>
          <div>
            <dt>规则回放</dt>
            <dd>{{ data?.coverage.start }} — {{ data?.coverage.end }}</dd>
          </div>
          <div>
            <dt>信号日</dt>
            <dd>
              {{ data?.coverage.signal_sessions }} 日 / 充分市场历史
              {{ data?.coverage.market_sessions }} 日
            </dd>
          </div>
          <div>
            <dt>模拟资金</dt>
            <dd>80 万元，最多 8 只，每笔预算 10 万元</dd>
          </div>
          <div>
            <dt>生成时间</dt>
            <dd>{{ localTime(data?.generated_at) }}</dd>
          </div>
        </dl>
        <router-link :to="{ path: '/strategy-review/orders', query: { dataset: 'original' } }"
          >查看逐笔信号与成交依据</router-link
        >
      </section>
      <section class="panel detail-panel">
        <h2 class="section-heading">上线验收</h2>
        <p class="data-note">门槛在观察收益结果前固定。</p>
        <ul class="criteria">
          <li>
            两组均至少
            {{ data?.policy.minimum_test_dates }} 个有完成买卖的样本外信号日、{{
              data?.policy.minimum_closed_orders
            }}
            笔买卖
          </li>
          <li>
            可核验执行覆盖至少
            {{ percent(data?.policy.minimum_execution_coverage, true, 0) }}
          </li>
          <li>收益改善置信区间下界为正，组合回撤与双倍滑点检查通过</li>
          <li>历史股票状态、行业归属和价格基础均可核验</li>
        </ul>
      </section>
    </div>
    <section v-if="uniqueReasons.length" class="panel detail-panel section">
      <n-collapse
        ><n-collapse-item title="未通过的条件" name="reasons"
          ><ul class="criteria">
            <li v-for="reason in uniqueReasons" :key="reason">
              {{ reasons[reason] || reason }}
            </li>
          </ul></n-collapse-item
        ></n-collapse
      >
    </section>
    <p class="data-note section">
      历史信号由规则截断重建。原 AI
      名单的真实发布时间及表现另列于完整报告。当前行业与股票状态缺少历史版本，历史价格存在混合来源。
    </p>
  </page-state>
</template>

<style scoped>
.section {
  margin-top: 22px;
}
.entry-table-wrap {
  overflow-x: auto;
}
.entry-table {
  border-collapse: collapse;
  width: 100%;
  white-space: nowrap;
}
.entry-table th, .entry-table td {
  padding: 8px 12px;
  text-align: right;
}
.entry-table th:first-child {
  text-align: left;
}
.review-status p {
  margin: 6px 0 0;
  font-size: 13px;
}
.review-window {
  display: flex;
  gap: 24px;
  flex-wrap: wrap;
  margin-top: 20px;
}
.review-window strong {
  color: var(--color-text);
  font-weight: 500;
}
.panel-heading {
  padding: 21px 22px 16px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
}
.panel-heading .data-note {
  margin: 7px 0 0;
}
.table-note {
  padding: 16px 22px;
  margin: 0;
}
.review-details {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 18px;
}
.detail-panel {
  padding: 23px;
}
.detail-panel dl {
  margin: 20px 0;
  display: grid;
  gap: 15px;
}
.detail-panel dl > div {
  display: flex;
  gap: 20px;
  justify-content: space-between;
  font-size: 13px;
}
.detail-panel dt {
  color: var(--color-text-muted);
  flex: 0 0 auto;
}
.detail-panel dd {
  margin: 0;
  text-align: right;
  font-variant-numeric: tabular-nums;
}
.criteria {
  font-size: 13px;
  line-height: 1.8;
  color: var(--color-text-muted);
  padding-left: 19px;
  margin: 14px 0 0;
}
.criteria li {
  margin: 9px 0;
}
.detail-panel > .data-note {
  margin: 8px 0;
}
.panel :deep(.strategy-name) {
  display: flex;
  flex-direction: column;
  gap: 5px;
  font-size: 13px;
}
.panel :deep(.strategy-name strong) {
  font-weight: 500;
}
.panel :deep(.strategy-name .data-note) {
  font-size: 11px;
}
@media (max-width: 850px) {
  .review-details {
    grid-template-columns: 1fr;
  }
}
@media (max-width: 640px) {
  .panel-heading,
  .detail-panel {
    padding: 17px;
  }
  .review-window {
    gap: 10px;
  }
  .table-note {
    padding: 14px 17px;
  }
  .detail-panel dl > div {
    flex-wrap: wrap;
    gap: 5px;
  }
  .detail-panel dd {
    text-align: left;
  }
}
</style>
