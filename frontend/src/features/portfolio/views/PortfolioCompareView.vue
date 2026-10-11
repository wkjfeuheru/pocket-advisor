<script setup lang="ts">
/**
 * 用户持仓与 AI 账户的收益对比页。
 *
 * 这是"实时展示收益对比"的落点：两条线在同一张图上、同一口径下比较。
 * 本页**只读**——采纳方案的入口在对话里（`allocation_required` 中断）。
 *
 * 三条展示纪律：
 * - 归一净值曲线为主（都以 1.0 为起点），绝对金额只作辅助；
 * - 每个数字都带 ``as_of`` 与 ``limitations``，不显示没有出处的数；
 * - 图表库缺失或接口失败时降级成表格，而不是整页空白。
 */
import { computed, onBeforeUnmount, onMounted, ref, shallowRef, watch } from 'vue'
import { http } from '@/api/client'
import { formatAmount } from '@/features/chat/artifacts.ts'

interface CurvePoint {
  date: string
  unit_nav: number
}

const loading = ref(false)
const errorText = ref('')
const account = ref<Record<string, any> | null>(null)
const aiCurve = ref<CurvePoint[]>([])
const userCurve = ref<CurvePoint[]>([])
const limitations = ref<string[]>([])
const navAsOf = ref<string | null>(null)

const chartEl = ref<HTMLElement | null>(null)
const chart = shallowRef<any>(null)
const chartUnavailable = ref(false)

const hasCurve = computed(() => aiCurve.value.length > 1 || userCurve.value.length > 1)

function seriesFrom(points: CurvePoint[]) {
  return points.map((p) => [p.date, p.unit_nav])
}

async function renderChart() {
  if (!chartEl.value || !hasCurve.value) return
  try {
    const echarts = await import('echarts')
    if (!chart.value) {
      chart.value = echarts.init(chartEl.value)
    }
    chart.value.setOption({
      tooltip: { trigger: 'axis' },
      legend: { data: ['AI 账户', '我的持仓'] },
      grid: { left: 48, right: 16, top: 32, bottom: 32 },
      xAxis: { type: 'category', boundaryGap: false },
      yAxis: { type: 'value', scale: true, name: '归一净值' },
      series: [
        {
          name: 'AI 账户',
          type: 'line',
          showSymbol: false,
          data: seriesFrom(aiCurve.value),
        },
        {
          name: '我的持仓',
          type: 'line',
          showSymbol: false,
          data: seriesFrom(userCurve.value),
        },
      ],
    })
  } catch {
    // 图表库不可用（未安装或加载失败）时降级成表格，不阻断页面。
    chartUnavailable.value = true
  }
}

async function load() {
  loading.value = true
  errorText.value = ''
  try {
    const { data } = await http.get('/portfolio/ai-account')
    account.value = data
    limitations.value = Array.isArray(data?.limitations) ? data.limitations : []
    navAsOf.value = data?.nav_as_of ?? null
    aiCurve.value = Array.isArray(data?.ai_curve) ? data.ai_curve : []
    userCurve.value = Array.isArray(data?.user_curve) ? data.user_curve : []
    await renderChart()
  } catch (exc: any) {
    errorText.value = exc?.response?.data?.detail || '读取收益对比失败'
  } finally {
    loading.value = false
  }
}

function onResize() {
  chart.value?.resize()
}

onMounted(() => {
  load()
  window.addEventListener('resize', onResize)
})

onBeforeUnmount(() => {
  window.removeEventListener('resize', onResize)
  chart.value?.dispose()
})

watch([aiCurve, userCurve], () => {
  renderChart()
})
</script>

<template>
  <section class="compare-view">
    <header class="compare-head">
      <h2>收益对比</h2>
      <button type="button" class="refresh" :disabled="loading" @click="load">
        {{ loading ? '读取中…' : '刷新' }}
      </button>
    </header>

    <p v-if="errorText" class="compare-error">{{ errorText }}</p>

    <dl class="compare-metrics">
      <div>
        <dt>AI 账户单位净值</dt>
        <dd>{{ account?.ai_account?.unit_nav ?? '—' }}</dd>
      </div>
      <div>
        <dt>AI 账户总资产</dt>
        <dd>{{ account?.ai_account ? formatAmount(account.ai_account.total_assets ?? 0) : '—' }}</dd>
      </div>
      <div>
        <dt>我的总资产</dt>
        <dd>{{ account?.user_total_assets != null ? formatAmount(account.user_total_assets) : '—' }}</dd>
      </div>
      <div>
        <dt>数据截止</dt>
        <dd>{{ navAsOf || '—' }}</dd>
      </div>
    </dl>

    <div v-if="!account?.ai_account" class="compare-empty">
      AI 账户尚未建立。在对话里请求配置建议并采纳一个方案后，这里会开始记录对比曲线。
    </div>

    <div v-else>
      <div v-show="hasCurve && !chartUnavailable" ref="chartEl" class="compare-chart"></div>

      <p v-if="chartUnavailable" class="compare-note">
        图表库不可用，已改用下方数据表展示。
      </p>

      <table v-if="hasCurve" class="compare-table">
        <thead>
          <tr><th>日期</th><th>AI 账户</th><th>我的持仓</th></tr>
        </thead>
        <tbody>
          <tr v-for="point in aiCurve" :key="point.date">
            <td>{{ point.date }}</td>
            <td>{{ point.unit_nav.toFixed(4) }}</td>
            <td>
              {{ (userCurve.find((item) => item.date === point.date)?.unit_nav ?? 0).toFixed(4) }}
            </td>
          </tr>
        </tbody>
      </table>

      <ul v-if="limitations.length" class="compare-limits">
        <li v-for="note in limitations" :key="note">{{ note }}</li>
      </ul>
    </div>
  </section>
</template>

<style scoped>
.compare-view { padding: 28px; max-width: 1000px; }
.compare-head { display: flex; justify-content: space-between; align-items: center; gap: 16px; }
.compare-head h2 {
  margin: 0;
  font-family: var(--font-display);
  font-size: var(--text-2xl);
  font-weight: 600;
  color: var(--color-primary);
}
.refresh {
  border: 1px solid var(--color-border);
  border-radius: var(--radius);
  background: var(--color-surface);
  color: var(--color-text-secondary);
  padding: 8px 14px;
  font-weight: 600;
  cursor: pointer;
}
.refresh:hover { border-color: var(--color-primary); color: var(--color-primary); }
.compare-error { color: var(--color-danger); font-size: 13px; }
.compare-metrics {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
  gap: 14px;
  margin: 18px 0;
}
.compare-metrics div {
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  background: var(--color-surface);
  box-shadow: var(--shadow-sm);
  padding: 14px 16px;
}
.compare-metrics dt { color: var(--color-text-muted); font-size: var(--text-xs); letter-spacing: 0.05em; }
.compare-metrics dd {
  margin: 6px 0 0;
  font-family: var(--font-display);
  font-size: 20px;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
}
.compare-chart {
  width: 100%;
  height: 320px;
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  background: var(--color-surface);
  box-shadow: var(--shadow-sm);
}
.compare-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
  margin-top: 12px;
  font-variant-numeric: tabular-nums;
}
.compare-table th {
  text-align: left;
  color: var(--color-primary);
  font-weight: 600;
  background: var(--color-primary-soft);
  border: 1px solid var(--color-hairline);
  padding: 7px 10px;
}
.compare-table td { border: 1px solid var(--color-hairline); padding: 7px 10px; }
.compare-table tbody tr:nth-child(even) { background: rgba(28, 42, 34, 0.025); }
.compare-limits { margin: 12px 0 0; padding-left: 1.1em; color: var(--color-text-muted); font-size: 12px; }
.compare-empty, .compare-note {
  color: var(--color-text-secondary);
  font-size: 13px;
  border: 1px dashed var(--color-border);
  border-radius: var(--radius-md);
  padding: 14px 16px;
  background: var(--color-surface);
}
</style>
