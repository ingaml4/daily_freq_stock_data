<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from "vue";
import { NTag } from "naive-ui";
import { getJson } from "../api";
import { formatNumber, formatPercent } from "../formatters";
import PageState from "./PageState.vue";
import { message } from "./presentation";

type Anchor = {
  role: string;
  symbol: string;
  name: string;
  pct_chg: number;
  amount_yuan: number;
  amount_ratio_5d: number | null;
  confirmation: {
    close_at_least: number;
    group_rising_ratio_at_least: number;
    group_relative_pct_points_at_least: number;
  };
  invalidation: {
    close_below: number;
    or_group_rising_ratio_below: number;
    and_group_relative_pct_points_below: number;
  };
};

type Mainline = {
  key: string;
  kind: string;
  name: string;
  evidence: {
    rising: number;
    sample: number;
    member_count: number;
    median_pct_chg: number;
    relative_pct_points: number;
    amount_yuan: number;
    amount_ratio_5d: number;
    follower_rising: number;
    follower_sample: number;
    strong_sessions_5d: number;
  };
  anchors: Anchor[];
  overlapping_groups: { key: string; name: string }[];
};

type MarketData = {
  date: string | null;
  dates: string[];
  status: "ready" | "waiting" | "degraded";
  publication_ready: boolean;
  market: { expected: number | null; active_bars: number; no_trade: number; missing: number | null } | null;
  coverage: { valid_price_amount_rows: number; industry_mapped_rows: number } | null;
  mappings: Record<string, { label: string; nodes: number; observed_at: string | null }>;
  benchmark: { sample: number; median_pct_chg: number } | null;
  mainlines: Mainline[];
  observations: { key: string; name: string; status: string }[];
  previous: { date: string; status: string; items: { key: string; name: string; status: string }[] } | null;
  gaps: string[];
};

const props = defineProps<{ date?: string }>();
const emit = defineEmits<{ dates: [dates: string[]] }>();
const data = ref<MarketData | null>(null);
const loading = ref(true);
const error = ref("");
let controller: AbortController | undefined;
const gaps: Record<string, string> = {
  concept_membership_unavailable: "概念成分未入库，当前仅评估有成分依据的分组。",
  industry_membership_unavailable: "该日缺少可用的申万行业成分。",
  missing_or_invalid_quotes: "同日行情仍有未确认证券。",
  unverified_snapshot_coverage: "该日没有可核验的行情覆盖记录。",
  after_close_snapshot_unverified: "收盘行情快照待核验。",
  awaiting_market_bars: "等待同日收盘行情。",
  no_trading_day_data: "暂无已记录交易日。",
  no_requested_trading_day: "该日期暂无交易日记录。",
  price_or_amount_coverage_incomplete: "有效涨跌与成交额覆盖不足。",
  recent_sessions_incomplete: "近期交易日价量样本不足。",
  market_coverage_incomplete: "同日行情覆盖待核验。",
};
const roles: Record<string, string> = {
  leader: "领涨",
  turnover_core: "成交额核心",
  follow_through: "接力验证",
};
const trend: Record<string, string> = {
  continuing: "延续",
  diverging: "分化",
  fading: "退潮",
  pending: "待核",
};
const amount = (value: number) => `${formatNumber(value / 100_000_000)} 亿`;
const mappingNotes = computed(() => Object.values(data.value?.mappings || {})
  .filter((mapping) => mapping.nodes > 0)
  .map((mapping) => `${mapping.label} ${mapping.nodes} 组 · ${mapping.observed_at || "日期未知"}`));

async function load() {
  controller?.abort();
  const current = new AbortController();
  controller = current;
  loading.value = true;
  error.value = "";
  try {
    const result = await getJson<MarketData>("/api/market-mainline", { date: props.date }, current.signal);
    if (current.signal.aborted) return;
    data.value = result;
    emit("dates", result.dates);
  } catch (e) {
    if (!current.signal.aborted) error.value = message(e);
  } finally {
    if (!current.signal.aborted) loading.value = false;
  }
}
watch(() => props.date, load, { immediate: true });
onBeforeUnmount(() => controller?.abort());
</script>

<template>
  <section class="panel mainline-panel" aria-labelledby="mainline-heading">
    <div class="mainline-heading">
      <h2 id="mainline-heading">市场主线与锚点</h2>
      <span v-if="data && !loading" class="data-note tabular">{{ data.date }}</span>
    </div>
    <page-state :loading="loading" :error="error" @retry="load">
      <template v-if="data">
        <div class="mainline-meta data-note">
          <span v-if="data.market">
            {{ data.market.active_bars }} 只有成交 · {{ data.market.no_trade }} 只无成交
            <template v-if="data.market.missing !== null"> · {{ data.market.missing }} 只未确认</template>
          </span>
          <span v-if="data.coverage">
            行业覆盖 {{ data.coverage.industry_mapped_rows }}/{{ data.coverage.valid_price_amount_rows }}
          </span>
          <span v-if="data.benchmark">市场中位涨幅 {{ formatPercent(data.benchmark.median_pct_chg) }}</span>
        </div>
        <p v-for="note in mappingNotes" :key="note" class="data-note">麦蕊成分 · {{ note }}</p>
        <p v-for="gap in data.gaps" :key="gap" class="data-note">{{ gaps[gap] || gap }}</p>
        <p v-if="!data.publication_ready" class="mainline-empty">
          {{ data.status === "waiting" ? "等待收盘数据核验" : "当日主线证据不足" }}
        </p>
        <p v-else-if="!data.mainlines.length" class="mainline-empty">当日没有达到确认条件的主线。</p>
        <article v-for="line in data.mainlines" :key="line.key" class="mainline-card">
          <div class="mainline-heading">
            <h3>{{ line.name }}</h3>
            <n-tag size="small" :bordered="false">{{ data.mappings[line.kind]?.label }}</n-tag>
          </div>
          <dl class="mainline-evidence tabular">
            <div><dt>上涨 / 有效 / 成分</dt><dd>{{ line.evidence.rising }} / {{ line.evidence.sample }} / {{ line.evidence.member_count }}</dd></div>
            <div><dt>中位涨幅 · 相对市场</dt><dd>{{ formatPercent(line.evidence.median_pct_chg) }} · +{{ formatNumber(line.evidence.relative_pct_points) }} 个百分点</dd></div>
            <div><dt>成交额 · 近五日倍数</dt><dd>{{ amount(line.evidence.amount_yuan) }} · {{ formatNumber(line.evidence.amount_ratio_5d) }}×</dd></div>
            <div><dt>跟涨 · 近五日强势</dt><dd>{{ line.evidence.follower_rising }}/{{ line.evidence.follower_sample }} · {{ line.evidence.strong_sessions_5d }} 日</dd></div>
          </dl>
          <p v-if="line.overlapping_groups.length" class="data-note">
            成分重叠 · {{ line.overlapping_groups.map((group) => group.name).join("、") }}
          </p>
          <p v-if="!line.anchors.length" class="data-note">当前没有满足身份和成交额条件的锚点。</p>
          <ul v-else class="anchor-list">
            <li v-for="anchor in line.anchors" :key="anchor.symbol">
              <div class="anchor-heading">
                <strong>{{ anchor.name }}</strong>
                <span class="data-note tabular">{{ anchor.symbol }}</span>
                <n-tag size="small" :bordered="false" type="info">{{ roles[anchor.role] || anchor.role }}</n-tag>
                <span class="tabular">{{ formatPercent(anchor.pct_chg) }} · {{ amount(anchor.amount_yuan) }}</span>
                <span v-if="anchor.amount_ratio_5d !== null" class="data-note">近五日 {{ formatNumber(anchor.amount_ratio_5d) }}×</span>
              </div>
              <p class="data-note">
                次日确认需收盘 ≥ {{ formatNumber(anchor.confirmation.close_at_least) }} 元，组内上涨占比 ≥
                {{ formatPercent(anchor.confirmation.group_rising_ratio_at_least, 0, true) }}，
                相对市场 ≥ {{ formatNumber(anchor.confirmation.group_relative_pct_points_at_least, 1) }} 个百分点。
              </p>
              <p class="data-note">
                失效条件为收盘低于 {{ formatNumber(anchor.invalidation.close_below) }} 元，或组内上涨占比低于
                {{ formatPercent(anchor.invalidation.or_group_rising_ratio_below, 0, true) }} 且跑输市场。
              </p>
            </li>
          </ul>
        </article>
        <p v-if="data.observations.length" class="data-note">
          未确认观察 · {{ data.observations.map((item) => item.name + "（" + (item.status === "pulse" ? "单日偏强" : "跟涨不足") + "）").join("、") }}
        </p>
        <div v-if="data.previous" class="previous-mainline">
          <strong>上一交易日 {{ data.previous.date }}</strong>
          <span v-if="data.previous.status === 'pending'" class="data-note">主线待核</span>
          <span v-else-if="!data.previous.items.length" class="data-note">无确认主线</span>
          <n-tag v-for="item in data.previous.items" :key="item.key" size="small" :bordered="false">
            {{ item.name }} · {{ trend[item.status] || item.status }}
          </n-tag>
        </div>
      </template>
    </page-state>
  </section>
</template>

<style scoped>
.mainline-panel { margin-top: 24px; padding: 22px 26px; }
.mainline-heading, .mainline-meta, .anchor-heading, .previous-mainline {
  display: flex; flex-wrap: wrap; align-items: center; gap: 8px 14px;
}
.mainline-heading h2, .mainline-heading h3 { margin: 0; }
.mainline-heading h2 { font-size: 18px; }
.mainline-heading h3 { font-size: 16px; }
.mainline-meta { margin-top: 12px; }
.mainline-panel p { margin: 7px 0 0; }
.mainline-empty { padding: 12px 0; }
.mainline-card, .previous-mainline { border-top: 1px solid var(--color-border); margin-top: 16px; padding-top: 14px; }
.mainline-evidence { display: grid; grid-template-columns: repeat(auto-fit, minmax(155px, 1fr)); gap: 10px; }
.mainline-evidence dt { font-size: 12px; color: var(--color-text-muted); }
.mainline-evidence dd { margin: 3px 0 0; }
.anchor-list { list-style: none; padding: 0; margin: 10px 0 0; }
.anchor-list li { padding: 10px 0; border-top: 1px solid var(--color-border); }
@media (max-width: 700px) { .mainline-panel { padding: 16px; } }
</style>
