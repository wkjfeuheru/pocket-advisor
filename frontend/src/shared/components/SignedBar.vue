<script setup lang="ts">
/**
 * 双向涨跌条：以中线为零点，向右表示上涨、向左表示下跌，用于指数/行业/概念的涨跌幅对比。
 *
 * 与项目既有配色保持一致（涨=绿、跌=红），幅度按同组最大值归一，避免不同量级被压成一条线。
 */
import { computed } from 'vue'

const props = defineProps<{
  label?: string
  /** 有符号数值（与 max 同单位，例如百分比）。 */
  value: number
  /** 同组最大绝对值，用于归一；<=0 时回退为 1。 */
  max?: number
  /** 右侧数值文案。 */
  display?: string
}>()

const numeric = computed(() => Number(props.value) || 0)
const bound = computed(() => Math.abs(Number(props.max)) || 1)
/** 单侧最大占轨道的 50%，因此比例再乘以 50。 */
const half = computed(() => `${Math.min(1, Math.abs(numeric.value) / bound.value) * 50}%`)
const tone = computed(() => (numeric.value > 0 ? 'up' : numeric.value < 0 ? 'down' : 'flat'))
const fillStyle = computed(() =>
  numeric.value >= 0 ? { left: '50%', width: half.value } : { right: '50%', width: half.value },
)
</script>

<template>
  <div class="signed" :class="`tone-${tone}`">
    <span v-if="label" class="signed-label">{{ label }}</span>
    <span class="signed-track">
      <i class="signed-zero"></i>
      <i class="signed-fill" :style="fillStyle"></i>
    </span>
    <span v-if="display != null && display !== ''" class="signed-value">{{ display }}</span>
  </div>
</template>

<style scoped>
.signed {
  display: grid;
  grid-template-columns: minmax(60px, 1fr) 2.2fr auto;
  align-items: center;
  gap: 10px;
  font-size: var(--text-sm);
}
.signed-label {
  color: var(--color-text-secondary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.signed-track {
  position: relative;
  height: 8px;
  border-radius: var(--radius-sm);
  background: var(--color-surface-alt);
  box-shadow: inset 0 0 0 1px var(--color-hairline);
}
.signed-zero {
  position: absolute;
  left: 50%;
  top: -2px;
  bottom: -2px;
  width: 1px;
  background: var(--color-border);
}
.signed-fill {
  position: absolute;
  top: 0;
  bottom: 0;
  border-radius: var(--radius-sm);
  background: var(--fill);
  transform-origin: var(--origin, center);
  animation: signedGrow 0.5s cubic-bezier(0.22, 0.61, 0.36, 1) both;
}
.signed-value {
  font-family: var(--font-mono);
  font-variant-numeric: tabular-nums;
  min-width: 48px;
  text-align: right;
}

.tone-up { --fill: var(--color-success); }
.tone-up .signed-fill { --origin: left center; }
.tone-up .signed-value { color: var(--color-success); }
.tone-down { --fill: var(--color-danger); }
.tone-down .signed-fill { --origin: right center; }
.tone-down .signed-value { color: var(--color-danger); }
.tone-flat { --fill: var(--color-text-muted); }
.tone-flat .signed-value { color: var(--color-text-secondary); }

@keyframes signedGrow {
  from { transform: scaleX(0); }
  to { transform: scaleX(1); }
}
@media (prefers-reduced-motion: reduce) {
  .signed-fill { animation: none; }
}
</style>
