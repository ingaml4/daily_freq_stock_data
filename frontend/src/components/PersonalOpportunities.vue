<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from "vue";
import { NButton, NTag } from "naive-ui";
import { getJson } from "../api";
import { formatNumber, formatPercent } from "../formatters";
import PageState from "./PageState.vue";
import { message } from "./presentation";

type Mode = { id: string; name: string; trigger: number; risk: number; basis: string };
type Mapping = { label: string; source: string; observed_at: string | null; nodes: number };
type Candidate = {
  secid: string; symbol: string; name: string; modes: Mode[];
  mainlines: { key: string; name: string; date: string; phase: string; membership_source: Mapping }[];
  bar: { date: string; close: number; pct_chg: number; volume_ratio_20: number; turnover: number | null; source: string; observed_at: string | null };
  macd: { state: string; structure_improving: boolean };
  pending_validation: string[];
};
type Payload = {
  date: string; status: string; candidates: Candidate[];
  coverage: { valid_current_bars: number; turnover_unknown: number; history_session_count: number; market: { observed_at: string | null; expected: number | null; missing: number | null; ready: boolean } | null };
  mainline: { publication_ready: boolean; previous_date: string | null; previous_publication_ready: boolean; gaps: string[]; mappings: Record<string, Mapping> };
  limitations: string[]; generated_at?: string; code_version?: string;
  provenance?: { mode: string; description: string };
};
const props = defineProps<{ date?: string }>();
const data = ref<Payload | null>(null);
const loading = ref(true);
const error = ref("");
const expanded = ref<Record<string, boolean>>({});
let controller: AbortController | undefined;
const modes = [
  { id: "A", title: "主线龙头主动转强 / 强势延续" },
  { id: "B", title: "主线调整后重新转强" },
  { id: "C", title: "长期横盘放量启动 / MACD 结构改善" },
];
const gapText: Record<string, string> = {
  concept_membership_unavailable: "概念成分缺失", industry_membership_unavailable: "行业成分缺失",
  missing_or_invalid_quotes: "行情有缺失或无效", unverified_snapshot_coverage: "行情覆盖未核验",
  after_close_snapshot_unverified: "收盘快照未核验", awaiting_market_bars: "等待当日行情",
  no_trading_day_data: "暂无交易日数据", no_requested_trading_day: "该日无交易日记录",
  price_or_amount_coverage_incomplete: "价格或成交额覆盖不足", recent_sessions_incomplete: "近期行情不完整",
  market_coverage_incomplete: "行情覆盖未核验",
};
const grouped = computed(() => modes.map(mode => ({ ...mode,
  candidates: (data.value?.candidates || []).filter(candidate => candidate.modes.some(item => item.id === mode.id)),
})));
const sourceName = (value: string | null | undefined) => {
  if (!value || value === "unknown") return "未知";
  if (value === "spot" || value === "mairui_kline") return "麦蕊日行情";
  if (value.toLowerCase().includes("mairui")) return "麦蕊";
  if (value.toLowerCase().includes("eastmoney")) return "东方财富";
  return "其他来源（待核）";
};
const num = (value: number) => formatNumber(value, 2);
async function load() {
  controller?.abort();
  const current = new AbortController();
  controller = current;
  loading.value = true;
  error.value = "";
  expanded.value = {};
  try {
    const result = await getJson<Payload>("/api/personal-opportunities", { date: props.date }, current.signal);
    if (current.signal.aborted) return;
    data.value = result;
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
  <section class="panel opportunity-panel" aria-labelledby="opportunity-heading">
    <div class="heading">
      <h2 id="opportunity-heading">个人机会准备</h2>
      <span v-if="data && !loading" class="data-note tabular">{{ data.date }}</span>
    </div>
    <page-state :loading="loading" :error="error" @retry="load">
      <template v-if="data">
        <p v-if="data.provenance" class="data-note">{{ data.provenance.mode === 'archived' ? '已保存归档' : '当前数据重算' }} · 生成于 {{ data.generated_at || '未知' }}。{{ data.provenance.description }}</p>
        <p class="intro">三种模式并列观察，以下是未经回测的准备清单，需继续验证，不能直接作为买入依据。</p>
        <div class="coverage data-note">
          <span>当日有效行情 {{ data.coverage.valid_current_bars }} 只</span>
          <span>换手率未知 {{ data.coverage.turnover_unknown }} 只</span>
          <span>行情覆盖{{ data.coverage.market?.ready ? "已核验" : "待核验" }}</span>
          <span>覆盖记录时间 {{ data.coverage.market?.observed_at || "未知" }}</span>
        </div>
        <p v-if="!data.mainline.publication_ready" class="data-note">当日主线尚未确认；涉及主线的判断以可核验的当日或前一交易日资料为准。</p>
        <p v-if="data.mainline.gaps.length" class="data-note">数据待核：{{ data.mainline.gaps.map(gap => gapText[gap] || "其他数据缺口待核").join("；") }}</p>
        <p v-if="data.status === 'waiting'" class="empty">该日期暂无可用行情，等待数据更新。</p>
        <template v-else>
          <div v-for="group in grouped" :key="group.id" class="mode-group">
            <div class="heading">
              <h3>{{ group.id }} · {{ group.title }}</h3>
              <span class="data-note">{{ group.candidates.length }} 只</span>
            </div>
            <p v-if="!group.candidates.length" class="data-note">当前无符合初版条件的候选；数据不完整时不能据此判断没有机会。</p>
            <article v-for="candidate in (expanded[group.id] ? group.candidates : group.candidates.slice(0, 5))" :key="candidate.secid" class="candidate">
              <div class="heading">
                <strong>{{ candidate.name }}</strong><span class="data-note tabular">{{ candidate.symbol }}</span>
                <n-tag size="small" :bordered="false" :type="candidate.macd.state === 'confirmed_daily' ? 'success' : 'warning'">
                  {{ candidate.macd.state === 'confirmed_daily' ? '日线已确认' : '盘中暂态或待核' }}
                </n-tag>
              </div>
              <dl class="metrics tabular">
                <div><dt>当前价格 · 涨幅</dt><dd>{{ num(candidate.bar.close) }} 元 · {{ formatPercent(candidate.bar.pct_chg) }}</dd></div>
                <div><dt>{{ group.id === 'C' ? '前 60 根有效日线箱顶' : '前 5 日日线收复位' }}</dt><dd>{{ num(candidate.modes.find(m => m.id === group.id)!.trigger) }} 元</dd></div>
                <div><dt>参考支撑风险位</dt><dd>{{ num(candidate.modes.find(m => m.id === group.id)!.risk) }} 元</dd></div>
                <div><dt>成交量 / 前 20 日中位数</dt><dd>{{ num(candidate.bar.volume_ratio_20) }} 倍</dd></div>
                <div><dt>换手率</dt><dd>{{ candidate.bar.turnover === null ? '未知' : formatPercent(candidate.bar.turnover) }}</dd></div>
                <div><dt>MACD 结构</dt><dd>{{ candidate.macd.structure_improving ? '较前日改善' : '未显示改善' }}</dd></div>
              </dl>
              <p class="data-note">入选依据：{{ candidate.modes.find(m => m.id === group.id)!.basis }}。</p>
              <p class="data-note" v-if="candidate.mainlines.length">主线依据：<span v-for="(line, index) in candidate.mainlines" :key="line.key + line.date">{{ index ? '；' : '' }}{{ line.name }}（{{ line.date }}，{{ sourceName(line.membership_source.source) }}成分，记录 {{ line.membership_source.observed_at || '时间未知' }}）</span></p>
              <p v-else class="data-note">主线成分未确认，待核。</p>
              <p class="data-note">行情来源：{{ sourceName(candidate.bar.source) }}；覆盖记录时间 {{ candidate.bar.observed_at || '未知' }}，单条行情采集时间未知。</p>
              <p class="data-note">待验证：{{ candidate.pending_validation.join('；') }}。</p>
            </article>
            <n-button v-if="group.candidates.length > 5" text type="primary" class="expand" @click="expanded[group.id] = !expanded[group.id]">
              {{ expanded[group.id] ? '收起' : `展开其余 ${group.candidates.length - 5} 只` }}
            </n-button>
          </div>
        </template>
        <details class="limitations"><summary>数据口径与局限</summary><p v-for="item in data.limitations" :key="item" class="data-note">{{ item }}</p></details>
      </template>
    </page-state>
  </section>
</template>

<style scoped>
.opportunity-panel { margin-top: 24px; padding: 22px 26px; }
.heading, .coverage { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 14px; }
.heading h2, .heading h3 { margin: 0; }
.heading h2 { font-size: 18px; }
.heading h3 { font-size: 16px; }
.opportunity-panel p { margin: 7px 0 0; overflow-wrap: anywhere; }
.intro { line-height: 1.6; }
.coverage { margin-top: 12px; }
.empty { padding: 12px 0; }
.mode-group { border-top: 1px solid var(--color-border); margin-top: 16px; padding-top: 14px; }
.candidate { border-top: 1px solid var(--color-border); padding: 12px 0; margin-top: 10px; min-width: 0; }
.metrics { display: grid; grid-template-columns: repeat(auto-fit, minmax(145px, 1fr)); gap: 10px; margin: 12px 0; }
.metrics div { min-width: 0; }
.metrics dt { font-size: 12px; color: var(--color-text-muted); }
.metrics dd { margin: 3px 0 0; }
.expand { margin-top: 8px; }
.limitations { border-top: 1px solid var(--color-border); margin-top: 16px; padding-top: 12px; }
.limitations summary { cursor: pointer; }
@media (max-width: 700px) { .opportunity-panel { padding: 16px; } .metrics { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
</style>
