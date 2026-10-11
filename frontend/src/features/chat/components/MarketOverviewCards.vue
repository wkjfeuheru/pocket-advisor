<script setup lang="ts">
/**
 * 大盘概览卡组：指数、广度、行业、概念、风险。
 *
 * 指数与行业/概念原本只是文字列表，这里改为以中线为零点的双向涨跌条，
 * 让"涨跌强弱"一眼可比；广度改为上涨/下跌堆叠条，一眼看出多空结构。
 */
import { computed } from 'vue'
import { marketOverviewView } from '../artifacts'
import SignedBar from '@/shared/components/SignedBar.vue'

const props = defineProps<{
  overview: Record<string, any>
}>()

const view = computed(() => marketOverviewView(props.overview))
const indices = computed<Record<string, any>[]>(() => props.overview.indices || [])
const breadth = computed<Record<string, any>>(() => props.overview.breadth || {})

/** 归一基准取同组最大绝对值，避免不同量级被压成一条线。 */
function scaleOf(items: Record<string, any>[]): number {
  const peak = items.reduce((max, item) => Math.max(max, Math.abs(Number(item.change_pct) || 0)), 0)
  return peak || 1
}
const indexScale = computed(() => scaleOf(indices.value))
const industryScale = computed(() => scaleOf(view.value.industries))
const conceptScale = computed(() => scaleOf(view.value.concepts))

/** 广度：上涨/下跌占比（总数为 0 时不做除法，避免 NaN）。 */
const breadthTotal = computed(() => (Number(breadth.value.up) || 0) + (Number(breadth.value.down) || 0))
const breadthWidths = computed(() => {
  const total = breadthTotal.value
  if (!total) return { up: 0, down: 0 }
  return {
    up: ((Number(breadth.value.up) || 0) / total) * 100,
    down: ((Number(breadth.value.down) || 0) / total) * 100,
  }
})

function signed(value: unknown): string {
  const numeric = Number(value)
  if (!Number.isFinite(numeric)) return '—'
  return `${numeric > 0 ? '+' : ''}${numeric.toFixed(2)}%`
}
</script>

<template>
  <div class="market-cards">
    <section class="card span-2">
      <h3>指数</h3>
      <div v-if="indices.length" class="stack">
        <SignedBar
          v-for="item in indices"
          :key="item.thscode || item.name"
          :label="item.name || item.thscode"
          :value="Number(item.change_pct) || 0"
          :max="indexScale"
          :display="item.change_pct == null ? '—' : signed(item.change_pct)"
        />
      </div>
      <p v-else class="empty">暂无指数快照</p>
    </section>

    <section class="card">
      <h3>市场广度</h3>
      <div class="breadth">
        <div class="breadth-bar" :title="`上涨 ${breadth.up ?? 0} / 下跌 ${breadth.down ?? 0}`">
          <i class="seg-up" :style="{ width: `${breadthWidths.up}%` }"></i>
          <i class="seg-down" :style="{ width: `${breadthWidths.down}%` }"></i>
        </div>
        <div class="breadth-legend">
          <span><i class="dot dot-up"></i>上涨 {{ breadth.up ?? '—' }}</span>
          <span><i class="dot dot-down"></i>下跌 {{ breadth.down ?? '—' }}</span>
        </div>
      </div>
      <ul class="chips">
        <li v-if="breadth.limit_up != null">涨停 {{ breadth.limit_up }}</li>
        <li v-if="breadth.limit_down != null">跌停 {{ breadth.limit_down }}</li>
        <li v-if="breadth.continue_board != null">连板 {{ breadth.continue_board }}</li>
        <li v-if="breadth.broken_board != null">炸板 {{ breadth.broken_board }}</li>
      </ul>
    </section>

    <section class="card">
      <h3>行业</h3>
      <div v-if="view.industries.length" class="stack">
        <SignedBar
          v-for="item in view.industries"
          :key="item.name"
          :label="item.name"
          :value="Number(item.change_pct) || 0"
          :max="industryScale"
          :display="item.change_pct == null ? '—' : signed(item.change_pct)"
        />
      </div>
      <p v-else class="empty">未提供行业数据</p>
    </section>

    <section class="card">
      <h3>概念</h3>
      <div v-if="view.concepts.length" class="stack">
        <SignedBar
          v-for="item in view.concepts"
          :key="item.name"
          :label="item.name"
          :value="Number(item.change_pct) || 0"
          :max="conceptScale"
          :display="item.change_pct == null ? '—' : signed(item.change_pct)"
        />
      </div>
      <p v-else class="empty">未提供概念数据</p>
    </section>

    <section class="card">
      <h3>风险与限制</h3>
      <ul v-if="view.risks.length" class="risks">
        <li v-for="item in view.risks" :key="item">{{ item }}</li>
      </ul>
      <!-- 无风险/限制时只给中性文案；数据截止时间由回答正文的「数据截止」承载，不在此重复。 -->
      <p v-else class="empty">本次未发现额外风险提示。</p>
    </section>
  </div>
</template>

<style scoped>
.market-cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 12px; }
.card {
  display: flex;
  flex-direction: column;
  gap: 12px;
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  padding: 14px 16px;
  background: var(--color-surface);
  box-shadow: var(--shadow-sm);
}
.span-2 { grid-column: span 2; }
@media (max-width: 640px) {
  .span-2 { grid-column: auto; }
}
h3 {
  margin: 0;
  padding-bottom: 8px;
  border-bottom: 1px solid var(--color-hairline);
  font-family: var(--font-display);
  font-size: 14px;
  color: var(--color-primary);
}
.stack { display: flex; flex-direction: column; gap: 8px; }
.empty { margin: 0; font-size: var(--text-sm); color: var(--color-text-muted); }

/* 广度堆叠条 */
.breadth { display: flex; flex-direction: column; gap: 8px; }
.breadth-bar {
  display: flex;
  height: 10px;
  border-radius: 999px;
  overflow: hidden;
  background: var(--color-surface-alt);
  box-shadow: inset 0 0 0 1px var(--color-hairline);
}
.seg-up { background: var(--color-success); }
.seg-down { background: var(--color-danger); }
.breadth-legend { display: flex; gap: 14px; font-size: var(--text-xs); color: var(--color-text-secondary); }
.breadth-legend span { display: inline-flex; align-items: center; gap: 6px; }
.dot { width: 7px; height: 7px; border-radius: 50%; }
.dot-up { background: var(--color-success); }
.dot-down { background: var(--color-danger); }

.chips { display: flex; flex-wrap: wrap; gap: 6px; margin: 0; padding: 0; list-style: none; }
.chips li {
  padding: 3px 9px;
  border: 1px solid var(--color-hairline);
  border-radius: 999px;
  background: var(--color-surface-alt);
  font: var(--text-xs) / 1.6 var(--font-mono);
  font-variant-numeric: tabular-nums;
  color: var(--color-text-secondary);
}

.risks { margin: 0; padding-left: 1.1em; font-size: var(--text-sm); color: var(--color-text-secondary); }
.risks li::marker { color: var(--color-accent); }
</style>
