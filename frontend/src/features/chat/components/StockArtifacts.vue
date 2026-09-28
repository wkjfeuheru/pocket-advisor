<script setup lang="ts">
import { computed } from 'vue'
import { primaryStockArtifact } from '../artifacts'
import MarketOverviewCards from './MarketOverviewCards.vue'
import EquityResearchPanel from './EquityResearchPanel.vue'
import CandidatePoolTable from './CandidatePoolTable.vue'

const props = defineProps<{
  artifacts?: unknown
}>()

const artifact = computed(() => primaryStockArtifact(props.artifacts))
</script>

<template>
  <div v-if="artifact" class="stock-artifacts">
    <MarketOverviewCards v-if="artifact.scenario === 'market_overview'" :overview="artifact.market_overview || artifact" />
    <EquityResearchPanel v-else-if="artifact.scenario === 'equity_research'" :equities="artifact.equity_research || []" />
    <CandidatePoolTable v-else-if="artifact.scenario === 'stock_screening'" :screening="artifact.stock_screening || artifact" />
  </div>
</template>
