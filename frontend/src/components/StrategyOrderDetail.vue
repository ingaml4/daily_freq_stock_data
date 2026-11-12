<script setup lang="ts">
import { computed } from "vue";
import { NCollapse, NCollapseItem, NDrawer, NDrawerContent } from "naive-ui";
import StrategyOrderExecution from "./StrategyOrderExecution.vue";
import { localTime, number } from "./presentation";
import { label, orderMode, policies, type OrderRow, type PairedOrders } from "../strategyOrders";

const props = defineProps<{ row: OrderRow | null }>();
const show = defineModel<boolean>("show", { required: true });

const candidate = computed(() => props.row?.dataset === "abc" ? props.row.raw : null);
const order = computed(() => props.row?.dataset === "original" ? props.row.raw : null);
const heading = computed(() => {
  if (!props.row) return "逐笔详情";
  const code = props.row.secid.split(".").at(-1);
  return props.row.name ? `${props.row.name} · ${code}` : code;
});
const rawJson = computed(() => props.row ? JSON.stringify(props.row.raw, null, 2) : "");

function executions(orders: PairedOrders) {
  return policies.flatMap((policy) => {
    const execution = orders[policy.id];
    return execution ? [{ ...policy, order: execution }] : [];
  });
}
</script>

<template>
  <n-drawer v-model:show="show" width="min(760px, 100vw)" placement="right">
    <n-drawer-content :title="heading" closable>
      <div v-if="row" class="detail-body">
        <p class="detail-subtitle data-note tabular">{{ row.date }} · {{ orderMode(row) }}</p>

        <section class="panel signal-evidence">
          <h3>信号与来源</h3>
          <p v-if="candidate?.mode_basis || order?.signal?.reason" class="signal-reason">
            {{ candidate?.mode_basis || order?.signal?.reason }}
          </p>
          <dl>
            <div>
              <dt>触发位</dt>
              <dd>{{ number(candidate?.trigger ?? order?.signal?.trigger_price) }}</dd>
            </div>
            <div>
              <dt>失效位</dt>
              <dd>{{ number(candidate?.risk ?? order?.invalidation_price) }}</dd>
            </div>
            <template v-if="candidate">
              <div>
                <dt>信号来源</dt>
                <dd>{{ label(candidate.origin) }}</dd>
              </div>
              <div v-if="candidate.signal_bar">
                <dt>信号日收盘</dt>
                <dd>{{ number(candidate.signal_bar.close) }} · {{ label(candidate.signal_bar.source) }}</dd>
              </div>
              <div v-if="candidate.signal_bar?.observed_at">
                <dt>行情记录时间</dt>
                <dd>{{ localTime(candidate.signal_bar.observed_at) }}</dd>
              </div>
              <div v-if="candidate.signal_bar?.volume_ratio_20 != null">
                <dt>信号日量比</dt>
                <dd>{{ number(candidate.signal_bar.volume_ratio_20) }} 倍 · 相对前 20 日成交量中位数</dd>
              </div>
              <div v-if="candidate.history">
                <dt>参考日线</dt>
                <dd>
                  {{ candidate.history.start }} 至 {{ candidate.history.end }} · {{ candidate.history.count }} 根
                  <span v-if="candidate.history.price_sources?.length">
                    · {{ candidate.history.price_sources.map(label).join("、") }}
                  </span>
                </dd>
              </div>
              <div v-if="candidate.macd">
                <dt>信号日 MACD</dt>
                <dd>{{ label(candidate.macd.state) }} · DIF {{ number(candidate.macd.dif, 4) }} · DEA {{ number(candidate.macd.dea, 4) }} · 柱值 {{ number(candidate.macd.histogram, 4) }}</dd>
              </div>
              <div v-if="candidate.entry_session">
                <dt>D+1 交易日</dt>
                <dd>{{ candidate.entry_session }}</dd>
              </div>
              <div v-if="candidate.target_date">
                <dt>D+5 交易日</dt>
                <dd>{{ candidate.target_date }}</dd>
              </div>
              <div v-if="candidate.generated_at">
                <dt>归档生成时间</dt>
                <dd>{{ localTime(candidate.generated_at) }}</dd>
              </div>
            </template>
            <template v-else-if="order">
              <div>
                <dt>信号规则</dt>
                <dd>{{ label(order.strategy) }}</dd>
              </div>
              <div v-if="order.signal?.indicator_asof">
                <dt>指标数据</dt>
                <dd>{{ order.signal.indicator_asof }} · {{ label(order.signal.indicator_basis) }}</dd>
              </div>
              <div v-if="order.invalidation_basis">
                <dt>失效位价格来源</dt>
                <dd>{{ label(order.invalidation_basis) }}</dd>
              </div>
              <div v-if="order.signal?.sector">
                <dt>行业</dt>
                <dd>{{ order.signal.sector }} · {{ label(order.signal.sector_asof) }}</dd>
              </div>
            </template>
          </dl>
        </section>

        <section v-if="order">
          <h3>执行记录</h3>
          <strategy-order-execution title="模拟成交" :order="order" />
        </section>
        <section v-if="candidate">
          <h3>D+5 固定退出</h3>
          <div class="execution-grid">
            <strategy-order-execution
              v-for="execution in executions(candidate.orders)"
              :key="execution.id"
              :title="execution.name"
              :order="execution.order"
            />
          </div>
          <n-collapse v-if="candidate.structure" class="structure-details">
            <n-collapse-item title="结构失效或 D+5 退出" name="structure">
              <div class="execution-grid">
                <strategy-order-execution
                  v-for="execution in executions(candidate.structure)"
                  :key="execution.id"
                  :title="execution.name"
                  :order="execution.order"
                />
              </div>
            </n-collapse-item>
          </n-collapse>
        </section>

        <details class="raw-detail">
          <summary>本条记录 JSON</summary>
          <pre>{{ rawJson }}</pre>
        </details>
      </div>
    </n-drawer-content>
  </n-drawer>
</template>

<style scoped>
.detail-body {
  display: grid;
  gap: 18px;
  padding-bottom: 14px;
}
.detail-subtitle {
  margin: 0;
}
.detail-body h3 {
  margin: 0 0 10px;
  font-size: 14px;
  font-weight: 600;
}
.signal-evidence {
  padding: 16px;
}
.signal-reason {
  margin: 0 0 12px;
  font-size: 13px;
}
.signal-evidence dl {
  display: grid;
  gap: 7px 14px;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  margin: 0;
  font-size: 12px;
}
.signal-evidence dl > div {
  display: grid;
  grid-template-columns: 90px minmax(0, 1fr);
  gap: 8px;
}
.signal-evidence dt {
  color: var(--color-text-muted);
}
.signal-evidence dd {
  margin: 0;
  overflow-wrap: anywhere;
}
.execution-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 10px;
}
.structure-details {
  margin-top: 14px;
}
.raw-detail summary {
  width: fit-content;
  color: var(--color-accent);
  cursor: pointer;
  font-size: 13px;
}
.raw-detail summary:focus-visible {
  outline: 2px solid var(--color-accent);
  outline-offset: 4px;
}
.raw-detail pre {
  max-height: 350px;
  padding: 12px;
  overflow: auto;
  border: 1px solid var(--color-border);
  border-radius: 7px;
  background: var(--color-bg);
  color: var(--color-text-muted);
  font-size: 11px;
}
@media (max-width: 620px) {
  .signal-evidence dl, .execution-grid {
    grid-template-columns: 1fr;
  }
}
</style>
