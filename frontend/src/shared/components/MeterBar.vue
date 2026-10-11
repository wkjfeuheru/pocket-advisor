<script setup lang="ts">
/**
 * 水平量度条：把 0~1 的比例渲染成细轨进度条，用于评分、覆盖率等"占比型"指标。
 *
 * 纯 CSS 实现（不依赖 echarts），因此在对话气泡这类频繁挂载/卸载的场景里没有
 * 图表实例的生命周期负担；挂载时以 transition 展开，兼顾观感与性能。
 */
import { computed } from 'vue'

const props = defineProps<{
  /** 左侧标签（可选）。 */
  label?: string
  /** 比例，0~1；超出范围会被夹紧。 */
  ratio: number
  /** 右侧数值文案（可选），数字统一走等宽字形。 */
  display?: string
  /** 语义色调：主色/金铜/涨/跌/中性。 */
  tone?: 'primary' | 'accent' | 'up' | 'down' | 'muted'
}>()

const width = computed(() => `${Math.max(0, Math.min(1, Number(props.ratio) || 0)) * 100}%`)
</script>

<template>
  <div class="meter" :class="`tone-${tone || 'primary'}`">
    <span v-if="label" class="meter-label">{{ label }}</span>
    <span class="meter-track"><i class="meter-fill" :style="{ width }"></i></span>
    <span v-if="display != null && display !== ''" class="meter-value">{{ display }}</span>
  </div>
</template>

<style scoped>
.meter {
  display: grid;
  grid-template-columns: minmax(52px, 0.9fr) 2.4fr auto;
  align-items: center;
  gap: 10px;
  font-size: var(--text-sm);
}
.meter-label {
  color: var(--color-text-secondary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.meter-track {
  position: relative;
  height: 7px;
  border-radius: 999px;
  background: var(--color-surface-alt);
  box-shadow: inset 0 0 0 1px var(--color-hairline);
  overflow: hidden;
}
.meter-fill {
  display: block;
  height: 100%;
  border-radius: 999px;
  background: linear-gradient(90deg, var(--fill-from), var(--fill-to));
  transform-origin: left center;
  animation: meterGrow 0.5s cubic-bezier(0.22, 0.61, 0.36, 1) both;
}
.meter-value {
  font-family: var(--font-mono);
  font-variant-numeric: tabular-nums;
  color: var(--color-text);
  min-width: 34px;
  text-align: right;
}

/* 色调 */
.tone-primary { --fill-from: var(--color-primary-light); --fill-to: var(--color-primary); }
.tone-accent { --fill-from: #d5ac67; --fill-to: var(--color-accent); }
.tone-up { --fill-from: #4f9a72; --fill-to: var(--color-success); }
.tone-down { --fill-from: #cf6a52; --fill-to: var(--color-danger); }
.tone-muted { --fill-from: #b9bdb4; --fill-to: var(--color-text-muted); }

@keyframes meterGrow {
  from { transform: scaleX(0); }
  to { transform: scaleX(1); }
}
@media (prefers-reduced-motion: reduce) {
  .meter-fill { animation: none; }
}
</style>
