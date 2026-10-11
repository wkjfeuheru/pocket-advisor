<script setup lang="ts">
/**
 * 个股研究面板：每只标的一张"研究卡"。
 *
 * 数据仍来自 ``chartData``（口径不变、可单测），这里只把原本渲染成文本的
 * 评分分项与 MACD/RSI 真正画出来：分项用水平量度条，RSI 用带 20/80 参考线的振荡计。
 *
 * 卡片数据在 script 里摊平（bars/trend/macd/rsi），一是避免模板中深链重复索引，
 * 二是让可空字段（rsi）在模板里能被清晰收窄。
 */
import { computed } from 'vue'
import { buildScoreCompareChart, buildTechnicalOverviewChart, technicalsToChartInput } from '../chartData'
import MeterBar from '@/shared/components/MeterBar.vue'

const props = defineProps<{
  equities: Record<string, any>[]
}>()

const rows = computed(() => (props.equities || []).slice(0, 3))

const scores = computed(() =>
  buildScoreCompareChart(rows.value.map((item) => ({
    thscode: item.thscode,
    components: item.components || item.scores || {},
  }))),
)
const technicals = computed(() => buildTechnicalOverviewChart(technicalsToChartInput(rows.value)))

/** 分项评分为 0~100 量纲，转成量度条需要的 0~1。 */
function ratio(value: number): number {
  return Math.max(0, Math.min(100, Number(value) || 0)) / 100
}

/** 摊平后的研究卡：分项、趋势、MACD 双杠与 RSI 全部挂到各自标的上。 */
const cards = computed(() => {
  const barsByCode = new Map<string, { metric: string; ratio: number; display: string }[]>(
    (scores.value?.items || []).map((item) => [item.code, item.bars]),
  )
  const techByCode = new Map(technicals.value.map((spec) => [spec.code, spec]))
  return rows.value.map((item) => {
    const tech = techByCode.get(item.thscode)
    const oscillator = tech?.oscillator ?? null
    return {
      thscode: String(item.thscode || ''),
      name: item.name || item.thscode,
      as_of: item.as_of as string | undefined,
      last: item.quote?.last ?? null,
      roe: item.fundamentals?.roe ?? null,
      grossMargin: item.fundamentals?.gross_margin ?? null,
      warnings: (item.warnings || []) as string[],
      bars: barsByCode.get(item.thscode) || [],
      trend: tech?.summary?.trend || '',
      macd: tech?.macdBars || [],
      rsi: oscillator ? oscillator.value : null,
      rsiRatio: oscillator ? oscillator.ratio : 0,
    }
  })
})
</script>

<template>
  <div class="research">
    <p v-if="scores?.label" class="research-caption">{{ scores.label }}</p>

    <div class="research-grid">
      <article v-for="card in cards" :key="card.thscode" class="panel">
        <header class="panel-head">
          <h3>{{ card.name }}</h3>
          <span class="code">{{ card.thscode }}</span>
          <time v-if="card.as_of">{{ card.as_of }}</time>
        </header>

        <!-- 关键事实：数字统一等宽，缺失显示"不可用"而不是编造 -->
        <dl class="facts">
          <div>
            <dt>现价</dt>
            <dd>{{ card.last ?? '不可用' }}</dd>
          </div>
          <div>
            <dt>ROE</dt>
            <dd>{{ card.roe ?? '不可用' }}</dd>
          </div>
          <div>
            <dt>毛利率</dt>
            <dd>{{ card.grossMargin ?? '不可用' }}</dd>
          </div>
        </dl>

        <!-- 评分分项 -->
        <section v-if="card.bars.length" class="block">
          <h4>评分分项</h4>
          <MeterBar
            v-for="bar in card.bars"
            :key="bar.metric"
            :label="bar.metric"
            :ratio="ratio(bar.ratio)"
            :display="bar.display"
            tone="primary"
          />
        </section>

        <!-- 技术指标：MACD 双杠 + RSI 振荡计 -->
        <section v-if="card.trend || card.macd.length || card.rsi !== null" class="block">
          <h4>技术指标</h4>
          <p v-if="card.trend" class="trend">趋势 <strong>{{ card.trend }}</strong></p>

          <div v-if="card.macd.length" class="macd">
            <span class="mini-label">MACD</span>
            <span class="macd-track">
              <i
                v-for="(bar, index) in card.macd"
                :key="index"
                class="macd-bar"
                :style="{ width: `${bar.ratio}%` }"
              ></i>
            </span>
            <span class="mini-value">DIF / DEA</span>
          </div>

          <div v-if="card.rsi !== null" class="rsi">
            <span class="mini-label">RSI6</span>
            <span class="rsi-track">
              <i class="rsi-zone" style="left: 20%"></i>
              <i class="rsi-zone" style="left: 80%"></i>
              <i class="rsi-fill" :style="{ width: `${card.rsiRatio}%` }"></i>
            </span>
            <span class="mini-value">{{ card.rsi.toFixed(1) }}</span>
          </div>
        </section>

        <p v-if="card.warnings.length" class="warn">{{ card.warnings.join('；') }}</p>
      </article>
    </div>
  </div>
</template>

<style scoped>
.research { display: flex; flex-direction: column; gap: 10px; }
.research-caption {
  margin: 0;
  font-family: var(--font-mono);
  font-size: var(--text-xs);
  letter-spacing: 0.14em;
  text-transform: uppercase;
  color: var(--color-text-muted);
}
.research-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 12px; }

.panel {
  display: flex;
  flex-direction: column;
  gap: 12px;
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  padding: 14px 16px;
  background: var(--color-surface);
  box-shadow: var(--shadow-sm);
}
.panel-head { display: flex; align-items: baseline; gap: 8px; flex-wrap: wrap; }
.panel-head h3 {
  margin: 0;
  font-family: var(--font-display);
  font-size: 16px;
  font-weight: 600;
  color: var(--color-primary);
}
.code { color: var(--color-text-secondary); font: var(--text-xs) / 1 var(--font-mono); }
.panel-head time { margin-left: auto; color: var(--color-text-muted); font: var(--text-xs) / 1 var(--font-mono); }

/* 关键事实：三等分，数字等宽 */
.facts { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; margin: 0; }
.facts div { display: flex; flex-direction: column; gap: 2px; }
.facts dt { color: var(--color-text-muted); font-size: var(--text-xs); }
.facts dd {
  margin: 0;
  font-family: var(--font-mono);
  font-variant-numeric: tabular-nums;
  font-size: 13px;
}

.block { display: flex; flex-direction: column; gap: 8px; padding-top: 10px; border-top: 1px dashed var(--color-hairline); }
.block h4 {
  margin: 0;
  font-family: var(--font-mono);
  font-size: var(--text-xs);
  letter-spacing: 0.12em;
  text-transform: uppercase;
  color: var(--color-text-muted);
}
.trend { margin: 0; font-size: 13px; color: var(--color-text-secondary); }
.trend strong { color: var(--color-primary); }

/* MACD 双杠 */
.macd,
.rsi { display: grid; grid-template-columns: 44px 1fr auto; align-items: center; gap: 10px; }
.mini-label { color: var(--color-text-secondary); font-size: var(--text-xs); }
.mini-value {
  font-family: var(--font-mono);
  font-variant-numeric: tabular-nums;
  font-size: var(--text-xs);
  color: var(--color-text);
  min-width: 46px;
  text-align: right;
}
.macd-track { display: flex; flex-direction: column; gap: 3px; }
.macd-bar { display: block; height: 4px; border-radius: 999px; background: linear-gradient(90deg, #d5ac67, var(--color-accent)); }
.macd-bar:nth-child(2) { background: linear-gradient(90deg, var(--color-primary-light), var(--color-primary)); }

/* RSI 振荡计：20/80 参考线 */
.rsi-track {
  position: relative;
  height: 8px;
  border-radius: 999px;
  background: var(--color-surface-alt);
  box-shadow: inset 0 0 0 1px var(--color-hairline);
  overflow: hidden;
}
.rsi-zone { position: absolute; top: 0; bottom: 0; width: 1px; background: var(--color-border); }
.rsi-fill { position: absolute; left: 0; top: 0; bottom: 0; background: linear-gradient(90deg, var(--color-primary-light), var(--color-primary)); }

.warn { margin: 0; color: var(--color-danger); font-size: var(--text-xs); }
</style>
