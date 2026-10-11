<script setup lang="ts">
/**
 * 选股候选池：在保留溯源信息（来源/截止时间/快照）的前提下，把总分做成可视化进度条，
 * 分项拆成可扫读的筹码，并支持按总分在"服务端排名 / 降序 / 升序"之间切换。
 *
 * 默认沿用服务端给出的排名顺序（它才是权威排序），只有用户显式点击表头才重排。
 */
import { computed, ref } from 'vue'
import MeterBar from '@/shared/components/MeterBar.vue'

const props = defineProps<{
  screening: Record<string, any>
}>()

/** null = 服务端原始排名；true = 降序；false = 升序。 */
const sortDesc = ref<boolean | null>(null)

const candidates = computed<Record<string, any>[]>(() => props.screening?.candidates || [])

const rows = computed(() => {
  const list = [...candidates.value]
  if (sortDesc.value === null) return list
  const factor = sortDesc.value ? 1 : -1
  return list.sort((left, right) => ((Number(right.score) || 0) - (Number(left.score) || 0)) * factor)
})

const sortLabel = computed(() =>
  sortDesc.value === null ? '原始' : sortDesc.value ? '降序' : '升序',
)

function toggleSort() {
  sortDesc.value = sortDesc.value === null ? true : sortDesc.value ? false : null
}

/** 评分按百分制归一；超出范围会夹紧，避免异常值把条撑破。 */
function scoreRatio(score: unknown): number {
  return Math.min(1, Math.max(0, (Number(score) || 0) / 100))
}

/** 分项只展示前 4 项，其余折叠成 "+n"，避免单元格被撑长。 */
function componentChips(item: Record<string, any>) {
  const entries = Object.entries(item.components || {})
  return { shown: entries.slice(0, 4), extra: Math.max(0, entries.length - 4) }
}
</script>

<template>
  <div class="pool">
    <table class="candidates">
      <thead>
        <tr>
          <th>标的</th>
          <th class="sortable" @click="toggleSort">
            评分 <span class="sort-hint">{{ sortLabel }}</span>
          </th>
          <th>分项</th>
          <th>风险</th>
          <th>溯源</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="item in rows" :key="item.thscode">
          <td class="code">{{ item.thscode }}</td>
          <td class="score">
            <MeterBar :ratio="scoreRatio(item.score)" :display="String(item.score ?? '—')" tone="accent" />
          </td>
          <td>
            <div class="chips">
              <span v-for="[key, value] in componentChips(item).shown" :key="key" class="chip">
                {{ key }}<b>{{ value }}</b>
              </span>
              <span v-if="componentChips(item).extra" class="chip chip-more">
                +{{ componentChips(item).extra }}
              </span>
            </div>
          </td>
          <td class="risk">{{ (item.risks || []).join('；') || '—' }}</td>
          <td class="provenance">
            <span>{{ item.source || 'snapshot' }}</span>
            <span>{{ item.as_of || '—' }}</span>
            <span>{{ item.snapshot_id || '—' }}</span>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td class="empty" colspan="5">没有候选标的</td>
        </tr>
      </tbody>
    </table>
  </div>
</template>

<style scoped>
.pool { overflow-x: auto; }
.candidates {
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
  font-variant-numeric: tabular-nums;
}
th,
td {
  border-bottom: 1px solid var(--color-hairline);
  padding: 8px 10px;
  text-align: left;
  vertical-align: top;
}
th {
  background: var(--color-primary-soft);
  color: var(--color-primary);
  font-weight: 600;
  white-space: nowrap;
}
tbody tr:nth-child(even) { background: rgba(28, 42, 34, 0.025); }

.sortable { cursor: pointer; user-select: none; }
.sortable:hover { color: var(--color-primary-light); }
.sort-hint {
  margin-left: 6px;
  padding: 1px 6px;
  border-radius: 999px;
  background: var(--color-surface);
  color: var(--color-text-muted);
  font: var(--text-xs) / 1.6 var(--font-mono);
}

.code { font-family: var(--font-mono); color: var(--color-text); white-space: nowrap; }
/* 评分列固定宽度，量度条在单元格内自适应 */
.score { min-width: 168px; }
.score :deep(.meter) { grid-template-columns: 1fr auto; }

.chips { display: flex; flex-wrap: wrap; gap: 4px; }
.chip {
  display: inline-flex;
  align-items: baseline;
  gap: 4px;
  padding: 2px 7px;
  border: 1px solid var(--color-hairline);
  border-radius: 999px;
  background: var(--color-surface-alt);
  color: var(--color-text-secondary);
  font-size: var(--text-xs);
  white-space: nowrap;
}
.chip b { font-family: var(--font-mono); color: var(--color-text); }
.chip-more { color: var(--color-text-muted); }

.risk { color: var(--color-danger); max-width: 180px; }
.provenance { display: flex; flex-direction: column; gap: 2px; color: var(--color-text-muted); font-family: var(--font-mono); font-size: var(--text-xs); white-space: nowrap; }
.empty { color: var(--color-text-muted); text-align: center; }
</style>
