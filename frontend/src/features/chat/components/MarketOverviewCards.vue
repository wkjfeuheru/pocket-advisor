<script setup lang="ts">
import { computed } from 'vue'
import { marketOverviewView } from '../artifacts'

const props = defineProps<{
  overview: Record<string, any>
}>()
const view = computed(() => marketOverviewView(props.overview))
</script>

<template>
  <div class="market-cards">
    <section>
      <h3>指数</h3>
      <ul>
        <li v-for="item in overview.indices || []" :key="item.thscode">
          {{ item.name || item.thscode }}
          <strong v-if="item.change_pct != null">{{ item.change_pct }}%</strong>
        </li>
      </ul>
    </section>
    <section>
      <h3>广度</h3>
      <p>上涨 {{ overview.breadth?.up ?? '—' }} / 下跌 {{ overview.breadth?.down ?? '—' }}</p>
      <p>涨停 {{ overview.breadth?.limit_up ?? '—' }} / 跌停 {{ overview.breadth?.limit_down ?? '—' }}</p>
      <p>连板 {{ overview.breadth?.continue_board ?? '—' }} / 炸板 {{ overview.breadth?.broken_board ?? '—' }}</p>
    </section>
    <section>
      <h3>行业</h3>
      <ul>
        <li v-for="item in view.industries" :key="item.name">
          {{ item.name }}
          <strong v-if="item.change_pct != null">{{ item.change_pct }}%</strong>
        </li>
      </ul>
    </section>
    <section>
      <h3>概念</h3>
      <ul>
        <li v-for="item in view.concepts" :key="item.name">
          {{ item.name }}
          <strong v-if="item.change_pct != null">{{ item.change_pct }}%</strong>
        </li>
      </ul>
    </section>
    <section>
      <h3>风险</h3>
      <ul>
        <li v-for="item in view.risks" :key="item">{{ item }}</li>
      </ul>
      <p v-if="!view.risks.length">{{ overview.sentiment_input || overview.as_of || '按指数与广度观察，不编造涨跌。' }}</p>
    </section>
  </div>
</template>

<style scoped>
.market-cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; }
section { border: 1px solid var(--color-border); padding: 12px; background: var(--color-surface); }
h3 { margin: 0 0 8px; font-size: 14px; }
ul { margin: 0; padding-left: 16px; }
</style>
