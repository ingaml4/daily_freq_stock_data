<script setup lang="ts">
import { computed } from "vue";
import { NTag } from "naive-ui";
import { number, numeric, percent } from "./presentation";
import { label, statusTone, type Order } from "../strategyOrders";

const props = defineProps<{ title: string; order: Order }>();

const fills = computed(() => [
  { name: "模拟买入", fill: props.order.buy },
  { name: "模拟卖出", fill: props.order.sell },
]);
const attempts = computed(() => [
  ...props.order.entry_attempts.map((attempt) => ({ ...attempt, phase: "入场" })),
  ...props.order.exit_attempts.map((attempt) => ({ ...attempt, phase: "退出" })),
]);
const netReturn = computed(() => numeric(props.order.net_return));
</script>

<template>
  <article class="execution">
    <header class="execution-heading">
      <h4>{{ title }}</h4>
      <n-tag size="small" :bordered="false" :type="statusTone(order.status)">
        {{ label(order.status) }}
      </n-tag>
    </header>
    <p v-if="order.reason" class="execution-reason">{{ label(order.reason) }}</p>

    <div class="execution-prices">
      <div v-for="item in fills" :key="item.name">
        <span class="data-note">{{ item.name }}</span>
        <strong v-if="item.fill" class="tabular">¥{{ number(item.fill.price) }}</strong>
        <strong v-else>未成交</strong>
        <span v-if="item.fill" class="data-note tabular">
          {{ item.fill.date }} {{ label(item.fill.execution_phase) }} ·
          {{ number(item.fill.shares, 0) }} 股
        </span>
      </div>
      <div>
        <span class="data-note">扣费后净收益</span>
        <strong class="tabular" :class="{ up: netReturn !== null && netReturn > 0, down: netReturn !== null && netReturn < 0 }">
          {{ percent(netReturn) }}
        </strong>
        <span v-if="order.net_pnl != null" class="data-note tabular">
          净盈亏 {{ number(order.net_pnl) }} 元
        </span>
      </div>
    </div>

    <dl class="execution-facts">
      <div v-if="order.confirmation">
        <dt>确认日</dt>
        <dd>{{ order.confirmation.date }} · 触发位 {{ number(order.confirmation.trigger) }}</dd>
      </div>
      <div v-if="order.confirmation && (order.confirmation.open != null || order.confirmation.close != null)">
        <dt>确认日价格</dt>
        <dd>开盘 {{ number(order.confirmation.open) }} · 收盘 {{ number(order.confirmation.close) }}</dd>
      </div>
      <div v-if="order.confirmation?.reason && order.confirmation.reason !== order.reason">
        <dt>确认结果</dt>
        <dd>{{ label(order.confirmation.reason) }}</dd>
      </div>
      <div v-if="order.target_date">
        <dt>计划退出日</dt>
        <dd>{{ order.target_date }}</dd>
      </div>
      <div v-if="order.trigger_date">
        <dt>结构触发日</dt>
        <dd>{{ order.trigger_date }}</dd>
      </div>
      <div v-if="order.problem_date">
        <dt>数据异常日</dt>
        <dd>{{ order.problem_date }}</dd>
      </div>
      <div v-if="order.total_cost != null || order.cost != null">
        <dt>总交易成本</dt>
        <dd>{{ number(order.total_cost ?? order.cost) }} 元</dd>
      </div>
      <div>
        <dt>执行数据</dt>
        <dd>{{ label(order.quality) }} · {{ order.eligible_for_validation ? "可用于验证" : "未纳入验证" }}</dd>
      </div>
      <div>
        <dt>订单来源</dt>
        <dd>{{ label(order.provenance) }}</dd>
      </div>
      <div v-if="order.exit_rule_version">
        <dt>退出规则版本</dt>
        <dd>{{ order.exit_rule_version }}</dd>
      </div>
    </dl>

    <div v-if="attempts.length || order.path_gaps?.length" class="execution-blocks">
      <strong>成交阻塞与数据缺口</strong>
      <ul v-if="attempts.length">
        <li v-for="(attempt, index) in attempts" :key="index">
          <span class="data-note tabular">{{ attempt.date }} · {{ attempt.phase }}</span>
          <span>{{ label(attempt.reason) }}</span>
        </li>
      </ul>
      <p v-if="order.path_gaps?.length" class="data-note tabular">
        缺少交易日行情 {{ order.path_gaps.join("、") }}
      </p>
    </div>
  </article>
</template>

<style scoped>
.execution {
  min-width: 0;
  padding: 16px;
  border: 1px solid var(--color-border);
  border-radius: 9px;
  background: var(--color-surface);
}
.execution-heading {
  display: flex;
  flex-wrap: wrap;
  justify-content: space-between;
  align-items: center;
  gap: 6px 12px;
}
.execution-heading h4 {
  margin: 0;
  font-size: 14px;
  font-weight: 600;
}
.execution-reason {
  margin: 8px 0 0;
  color: var(--color-text-muted);
  font-size: 12px;
}
.execution-prices {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 10px;
  margin: 14px 0;
  padding: 11px 10px;
  border-radius: 7px;
  background: var(--color-surface-raised);
}
.execution-prices > div {
  display: grid;
  align-content: start;
}
.execution-prices strong {
  font-size: 14px;
  font-weight: 600;
  overflow-wrap: anywhere;
}
.execution-prices .data-note {
  font-size: 11px;
}
.execution-facts {
  display: grid;
  gap: 7px;
  margin: 0;
  font-size: 12px;
}
.execution-facts > div {
  display: grid;
  grid-template-columns: 88px minmax(0, 1fr);
  gap: 8px;
}
.execution-facts dt {
  color: var(--color-text-muted);
}
.execution-facts dd {
  margin: 0;
  overflow-wrap: anywhere;
}
.execution-blocks {
  margin-top: 13px;
  padding-top: 10px;
  border-top: 1px solid var(--color-border);
  font-size: 12px;
}
.execution-blocks ul {
  margin: 6px 0 0;
  padding: 0;
  list-style: none;
}
.execution-blocks li {
  display: grid;
  gap: 2px;
  padding: 3px 0;
}
.execution-blocks p {
  margin: 6px 0 0;
}
</style>
