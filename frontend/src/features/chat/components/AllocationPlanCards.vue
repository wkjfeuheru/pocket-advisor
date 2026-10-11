<script setup lang="ts">
/**
 * 持仓配置方案对比卡片。
 *
 * 展示的是**静态指标**：调仓明细、换手率、预估费用、集中度、限制说明。
 * 刻意不展示收益预测——方案是未来的事，给一个"预期收益"会被读成承诺。
 *
 * 数字全部来自服务端；本组件只做格式化，不做任何二次计算或"美化"。
 */
import { computed } from 'vue'
import {
  actionLabel,
  formatAmount,
  formatPercent,
  type AllocationPlan,
} from '../artifacts.ts'

const props = defineProps<{
  plans?: AllocationPlan[]
  /** 服务端声明但不可用的档位，用于说明"为什么这一档没有"。 */
  unavailable?: { tier: string; tier_label?: string; unavailable_reason?: string }[]
  disabled?: boolean
}>()

const emit = defineEmits<{ select: [planId: string] }>()

const planList = computed(() => (Array.isArray(props.plans) ? props.plans : []))
const unavailableList = computed(() =>
  Array.isArray(props.unavailable) ? props.unavailable : [],
)

const REASON_LABELS: Record<string, string> = {
  turnover_out_of_band: '换手率与当前持仓差距不合适',
  constraint_repaired: '在当前约束下无法同时满足换手率与持仓上限',
  no_candidate: '没有生成该档候选',
  risk_exceeded: '超出您的风险等级',
}

function reasonLabel(reason?: string): string {
  if (!reason) return ''
  return REASON_LABELS[reason] || reason
}

/** 调仓明细按变动幅度排序：先看动作大的，符合阅读习惯。 */
function sortedItems(plan: AllocationPlan) {
  return [...(plan.items || [])].sort(
    (a, b) => Math.abs(b.delta_weight ?? 0) - Math.abs(a.delta_weight ?? 0),
  )
}

function actionClass(action: string): string {
  if (action === 'open' || action === 'add') return 'is-buy'
  if (action === 'reduce' || action === 'close') return 'is-sell'
  return 'is-hold'
}
</script>

<template>
  <div class="allocation-plans">
    <div v-if="planList.length" class="plan-grid">
      <article v-for="plan in planList" :key="plan.plan_id" class="plan-card">
        <header class="plan-head">
          <span class="plan-tier">{{ plan.label || plan.tier }}</span>
          <span class="plan-turnover">换手 {{ formatPercent(plan.turnover_rate) }}</span>
        </header>

        <dl class="plan-metrics">
          <div>
            <dt>预估费用</dt>
            <dd>{{ formatAmount(plan.estimated_fee) }}</dd>
          </div>
          <div>
            <dt>调仓笔数</dt>
            <dd>{{ (plan.items || []).length }}</dd>
          </div>
        </dl>

        <p v-if="plan.summary" class="plan-summary">{{ plan.summary }}</p>

        <table class="plan-items">
          <thead>
            <tr>
              <th>标的</th>
              <th>动作</th>
              <th>目标权重</th>
              <th>理由</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="item in sortedItems(plan)" :key="plan.plan_id + item.product_code">
              <td class="code">{{ item.product_name || item.product_code }}</td>
              <td :class="actionClass(item.action)">{{ actionLabel(item.action) }}</td>
              <td>{{ item.target_weight === undefined ? '—' : formatPercent(item.target_weight) }}</td>
              <td class="reason">{{ item.reason || '—' }}</td>
            </tr>
          </tbody>
        </table>

        <ul v-if="plan.limitations && plan.limitations.length" class="plan-limits">
          <li v-for="note in plan.limitations" :key="note">{{ note }}</li>
        </ul>

        <button
          type="button"
          class="plan-adopt"
          :disabled="disabled"
          @click="emit('select', plan.plan_id)"
        >采纳此方案</button>
      </article>
    </div>

    <ul v-if="unavailableList.length" class="plan-unavailable">
      <li v-for="item in unavailableList" :key="item.tier">
        {{ item.tier_label || item.tier }}：{{ reasonLabel(item.unavailable_reason) }}
      </li>
    </ul>

    <p v-if="!planList.length && !unavailableList.length" class="plan-empty">
      暂无配置方案
    </p>
  </div>
</template>

<style scoped>
.allocation-plans { display: flex; flex-direction: column; gap: 12px; }
.plan-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 14px; }
.plan-card {
  display: flex;
  flex-direction: column;
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  padding: 14px 16px;
  background: var(--color-surface);
  box-shadow: var(--shadow-sm);
}
.plan-head { display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 10px; }
.plan-tier { font-family: var(--font-display); font-size: 16px; font-weight: 600; color: var(--color-primary); }
.plan-turnover { color: var(--color-text-secondary); font-size: 12px; font-variant-numeric: tabular-nums; }
.plan-metrics { display: flex; gap: 20px; margin: 0 0 10px; }
.plan-metrics dt { color: var(--color-text-muted); font-size: 11px; }
.plan-metrics dd { margin: 0; font-variant-numeric: tabular-nums; font-weight: 600; }
.plan-summary { color: var(--color-text-secondary); font-size: 13px; margin: 0 0 10px; }
.plan-items { width: 100%; border-collapse: collapse; font-size: 12px; font-variant-numeric: tabular-nums; }
.plan-items th {
  text-align: left;
  color: var(--color-text-muted);
  font-weight: 600;
  padding: 6px;
  border-bottom: 1px solid var(--color-hairline);
}
.plan-items td { padding: 6px; border-bottom: 1px solid var(--color-hairline); }
.plan-items .code { font-variant-numeric: tabular-nums; color: var(--color-text); }
.plan-items .reason { color: var(--color-text-secondary); }
/* 买入/加仓用红、卖出/减仓用绿，沿用 A 股配色习惯。 */
.is-buy { color: var(--color-danger); font-weight: 600; }
.is-sell { color: var(--color-success); font-weight: 600; }
.is-hold { color: var(--color-text-secondary); }
.plan-limits { margin: 8px 0 0; padding-left: 1.1em; color: var(--color-text-muted); font-size: 12px; }
.plan-unavailable { margin: 0; padding-left: 1.1em; color: var(--color-text-muted); font-size: 12px; }
.plan-empty { color: var(--color-text-muted); font-size: 13px; margin: 0; }
.plan-adopt {
  margin-top: auto;
  align-self: flex-start;
  border: 1px solid var(--color-primary);
  border-radius: var(--radius);
  background: var(--color-primary);
  color: #fff;
  padding: 9px 16px;
  font-weight: 600;
  cursor: pointer;
}
.plan-adopt:disabled { opacity: 0.5; cursor: not-allowed; }
</style>
