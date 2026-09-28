<script setup lang="ts">
import { computed } from 'vue'
import { buildScoreCompareChart, buildTechnicalOverviewChart, technicalsToChartInput } from '../chartData'

const props = defineProps<{
  equities: Record<string, any>[]
}>()

const rows = computed(() => (props.equities || []).slice(0, 3))
const scores = computed(() => buildScoreCompareChart(rows.value.map((item) => ({
  thscode: item.thscode,
  components: item.components || item.scores || {},
}))))
const technicals = computed(() => buildTechnicalOverviewChart(technicalsToChartInput(rows.value)))
</script>

<template>
  <div class="research-grid">
    <article v-for="item in rows" :key="item.thscode" class="panel">
      <h3>{{ item.name || item.thscode }}</h3>
      <p>截止 {{ item.as_of || '—' }}</p>
      <p v-if="item.quote?.last != null">现价 {{ item.quote.last }}</p>
      <p v-if="item.fundamentals">ROE {{ item.fundamentals.roe ?? '不可用' }} / 毛利率 {{ item.fundamentals.gross_margin ?? '不可用' }}</p>
      <p v-if="item.warnings?.length">{{ item.warnings.join('；') }}</p>
    </article>
    <div v-if="scores" class="scores">
      <div v-for="item in scores.items" :key="item.code">
        <strong>{{ item.code }}</strong>
        <span v-for="bar in item.bars" :key="bar.metric">{{ bar.metric }} {{ bar.display }}</span>
      </div>
    </div>
    <div v-if="technicals.length" class="technicals">
      <p v-for="spec in technicals" :key="spec.code">{{ spec.code }} {{ spec.summary.trend || '' }}</p>
    </div>
  </div>
</template>

<style scoped>
.research-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 12px; }
.panel { border: 1px solid var(--color-border); padding: 12px; }
h3 { margin: 0 0 8px; }
</style>
