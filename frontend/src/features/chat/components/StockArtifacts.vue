<script setup lang="ts">
import { computed } from 'vue'
import { publicArtifacts } from '../artifacts'
import MarketOverviewCards from './MarketOverviewCards.vue'
import EquityResearchPanel from './EquityResearchPanel.vue'
import CandidatePoolTable from './CandidatePoolTable.vue'

const props = defineProps<{
  artifacts?: unknown
}>()

/** 渲染本轮收到的**全部** artifact，而不是只取第一个，避免多标的结果被静默丢弃。 */
const items = computed(() => publicArtifacts(props.artifacts))
</script>

<template>
  <div v-if="items.length" class="stock-artifacts">
    <template v-for="(artifact, index) in items" :key="index">
      <MarketOverviewCards
        v-if="artifact.scenario === 'market_overview'"
        :overview="artifact.market_overview || artifact"
      />
      <EquityResearchPanel
        v-else-if="artifact.scenario === 'equity_research'"
        :equities="artifact.equity_research || []"
      />
      <CandidatePoolTable
        v-else-if="artifact.scenario === 'stock_screening'"
        :screening="artifact.stock_screening || artifact"
      />
    </template>
  </div>
</template>

<style scoped>
.stock-artifacts { display: flex; flex-direction: column; gap: 12px; }
</style>
