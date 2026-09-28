<script setup lang="ts">
defineProps<{
  screening: Record<string, any>
}>()

function componentText(item: Record<string, any>): string {
  const parts = item.components || {}
  return Object.entries(parts).map(([key, value]) => `${key}:${value}`).join(' ')
}
</script>

<template>
  <table class="candidates">
    <thead>
      <tr>
        <th>代码</th>
        <th>总分</th>
        <th>分项</th>
        <th>风险</th>
        <th>来源</th>
        <th>截止时间</th>
        <th>快照</th>
      </tr>
    </thead>
    <tbody>
      <tr v-for="item in screening.candidates || []" :key="item.thscode">
        <td>{{ item.thscode }}</td>
        <td>{{ item.score }}</td>
        <td>{{ componentText(item) }}</td>
        <td>{{ (item.risks || []).join('；') }}</td>
        <td>{{ item.source || 'snapshot' }}</td>
        <td>{{ item.as_of }}</td>
        <td>{{ item.snapshot_id }}</td>
      </tr>
    </tbody>
  </table>
</template>

<style scoped>
.candidates { width: 100%; border-collapse: collapse; font-size: 13px; }
th, td { border: 1px solid var(--color-border); padding: 6px 8px; text-align: left; }
</style>
