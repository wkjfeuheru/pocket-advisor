<script setup lang="ts">
/** 持仓：展示份额/成本/市值/浮动盈亏，支持部分或全部卖出，以及一键清仓。 */
import { ref, computed, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Refresh, Sell, Delete } from '@element-plus/icons-vue'
import { createOrder, liquidate, listPositions } from '@/features/portfolio/api'
import type { AccountSnapshot, PositionView } from '@/features/portfolio/types'
import type { UserInfo } from '@/features/auth/types'
import {
  describeLimitation, formatMoney, formatPercent, formatShare, formatShares,
  formatSignedMoney, makeIdempotencyKey, pnlClass,
} from '@/shared/format'

defineProps<{ currentUser: UserInfo }>()

const loading = ref(false)
const loadError = ref('')
const positions = ref<PositionView[]>([])
const account = ref<AccountSnapshot | null>(null)

const sellVisible = ref(false)
const selling = ref(false)
const target = ref<PositionView | null>(null)
const sellShares = ref<number>(0)
const sellAll = ref(false)
const sellKey = ref('')

/** 存在无法定价的持仓时，市值口径不完整，需要显式提示。 */
const pricingIssues = computed(() => account.value?.pricing_issues ?? [])
const hasUnpriced = computed(() => account.value?.market_value_complete === false)

/** 环形图配色：深绿系为主、金铜作强调，与整体设计令牌一致。 */
const DONUT_COLORS = ['#17362a', '#23543d', '#3f7a58', '#b9893f', '#d5ac67', '#8b9086']

/** 前 5 大持仓 + "其他"，占比按持仓权重归一，避免口径不完整时画不满一圈。 */
const donutItems = computed(() => {
  const ranked = positions.value
    .map((item) => ({
      name: item.product_name || item.product_code,
      weight: Number(item.weight) || 0,
    }))
    .filter((item) => item.weight > 0)
    .sort((left, right) => right.weight - left.weight)
  const top = ranked.slice(0, 5)
  const restWeight = ranked.slice(5).reduce((sum, item) => sum + item.weight, 0)
  const items = restWeight > 0 ? [...top, { name: '其他', weight: restWeight }] : top
  const total = items.reduce((sum, item) => sum + item.weight, 0) || 1
  return items.map((item) => ({ ...item, share: item.weight / total, weight: item.weight }))
})

/** 转成 SVG 圆环所需的分段（周长按 100 处理，dashoffset 从正上方起算）。 */
const donutSegments = computed(() => {
  let cursor = 0
  return donutItems.value.map((item, index) => {
    const segment = {
      ...item,
      color: DONUT_COLORS[index % DONUT_COLORS.length],
      dash: `${(item.share * 100).toFixed(3)} ${(100 - item.share * 100).toFixed(3)}`,
      offset: 25 - cursor * 100,
    }
    cursor += item.share
    return segment
  })
})

async function load() {
  loading.value = true
  loadError.value = ''
  try {
    const result = await listPositions()
    positions.value = result.positions
    account.value = result.account
  } catch (error: any) {
    positions.value = []
    account.value = null
    loadError.value = error?.message || '持仓加载失败'
    ElMessage.error(loadError.value)
  } finally {
    loading.value = false
  }
}

function openSell(item: PositionView) {
  target.value = item
  sellShares.value = item.shares
  sellAll.value = false
  sellKey.value = makeIdempotencyKey('sell')
  sellVisible.value = true
}

async function confirmSell() {
  if (!target.value) return
  if (!sellAll.value && (!sellShares.value || sellShares.value <= 0)) {
    ElMessage.warning('请输入大于 0 的赎回份额')
    return
  }
  selling.value = true
  try {
    const result = await createOrder({
      product_code: target.value.product_code,
      side: 'sell',
      shares: sellAll.value ? undefined : sellShares.value,
      all: sellAll.value,
      idempotency_key: sellKey.value,
    })
    ElMessage.success(
      `赎回成功：${result.order.shares} 份，到账 ${formatMoney(result.order.net_amount)} 元，`
      + `已实现盈亏 ${formatSignedMoney(result.order.realized_pnl)} 元`,
    )
    sellVisible.value = false
    await load()
  } catch (error: any) {
    ElMessage.error(error?.message || '赎回失败')
  } finally {
    selling.value = false
  }
}

/** 一键清仓：二次确认后清空全部持仓。 */
async function confirmLiquidate() {
  if (!positions.value.length) {
    ElMessage.info('当前没有持仓')
    return
  }
  try {
    await ElMessageBox.confirm(
      `确定要清仓全部 ${positions.value.length} 只持仓吗？将按最新净值全部赎回，该操作不可撤销。`,
      '一键清仓',
      { confirmButtonText: '确认清仓', cancelButtonText: '取消', type: 'warning' },
    )
  } catch {
    return
  }
  loading.value = true
  try {
    const result = await liquidate()
    const failedCodes = Object.keys(result.failed)
    ElMessage.success(
      `清仓完成：${result.cleared_codes.length} 只已赎回，`
      + `到账 ${formatMoney(result.total_cash_in)} 元，`
      + `已实现盈亏 ${formatSignedMoney(result.total_realized_pnl)} 元`,
    )
    if (failedCodes.length) {
      ElMessage.warning(
        `以下商品未能清仓：${failedCodes
          .map(code => `${code}（${describeLimitation(result.failed[code])}）`)
          .join('；')}`,
      )
    }
    await load()
  } catch (error: any) {
    ElMessage.error(error?.message || '清仓失败')
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>

<template>
  <section class="page">
    <header class="page-head">
      <div>
        <p class="eyebrow">PORTFOLIO / POSITIONS</p>
        <h2>我的持仓</h2>
        <p class="page-note">按最新净值估值；模拟交易数据，不构成投资建议。</p>
      </div>
      <div class="page-tools">
        <el-button :icon="Refresh" :loading="loading" @click="load">刷新</el-button>
        <el-button
          type="danger"
          :icon="Delete"
          :disabled="!positions.length"
          :loading="loading"
          @click="confirmLiquidate"
        >
          一键清仓
        </el-button>
      </div>
    </header>

    <!-- 口径不完整时必须明示，避免用户以为市值就是全部资产 -->
    <el-alert
      v-if="hasUnpriced"
      type="warning"
      show-icon
      :closable="false"
      class="warn-alert"
      title="部分持仓缺少可用净值，市值口径不完整"
      :description="`以下商品未计入市值：${pricingIssues.join('、')}`"
    />

    <div v-if="account" class="stat-grid">
      <div class="stat">
        <span class="stat-label">持仓市值</span>
        <strong>{{ formatMoney(account.market_value, '元') }}</strong>
      </div>
      <div class="stat">
        <span class="stat-label">浮动盈亏</span>
        <strong :class="pnlClass(account.position_pnl)">{{ formatSignedMoney(account.position_pnl) }} 元</strong>
      </div>
      <div class="stat">
        <span class="stat-label">持仓成本</span>
        <strong>{{ formatMoney(account.position_cost, '元') }}</strong>
      </div>
      <div class="stat">
        <span class="stat-label">持仓数量</span>
        <strong>{{ account.position_count }} 只</strong>
      </div>
    </div>

    <!-- 持仓结构：环形图给出集中度直觉，明细仍以表格为准 -->
    <section v-if="positions.length" class="allocation">
      <div class="allocation-head">
        <p class="eyebrow">ALLOCATION</p>
        <h3>持仓结构</h3>
      </div>
      <div class="allocation-body">
        <figure class="donut">
          <svg viewBox="0 0 42 42" role="img" aria-label="持仓占比分布">
            <circle class="donut-track" cx="21" cy="21" r="15.9155" />
            <circle
              v-for="segment in donutSegments"
              :key="segment.name"
              class="donut-seg"
              cx="21"
              cy="21"
              r="15.9155"
              :stroke="segment.color"
              :stroke-dasharray="segment.dash"
              :stroke-dashoffset="segment.offset"
            />
          </svg>
          <figcaption>
            <strong>{{ formatMoney(account?.market_value, '元') }}</strong>
            <span>持仓市值</span>
          </figcaption>
        </figure>
        <ul class="legend">
          <li v-for="segment in donutSegments" :key="segment.name">
            <i :style="{ background: segment.color }"></i>
            <span class="legend-name">{{ segment.name }}</span>
            <b>{{ formatShare(segment.weight) }}</b>
          </li>
        </ul>
      </div>
    </section>

    <el-card shadow="never" v-loading="loading" class="table-card">
      <el-empty
        v-if="!loading && loadError"
        :image-size="60"
        :description="loadError"
      />
      <el-empty
        v-else-if="!loading && !positions.length"
        :image-size="60"
        description="暂无持仓，去「商品」页面申购"
      />
      <el-table v-else :data="positions" stripe style="width: 100%">
        <el-table-column label="商品" min-width="180" show-overflow-tooltip>
          <template #default="{ row }">
            <div class="product-cell">
              <strong>{{ row.product_name || row.product_code }}</strong>
              <small class="mono">{{ row.product_code }}</small>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="份额" width="110" align="right">
          <template #default="{ row }">{{ formatShares(row.shares) }}</template>
        </el-table-column>
        <el-table-column label="持仓成本" width="120" align="right">
          <template #default="{ row }">{{ formatMoney(row.cost_amount) }}</template>
        </el-table-column>
        <el-table-column label="平均成本" width="100" align="right">
          <template #default="{ row }">{{ row.avg_cost.toFixed(4) }}</template>
        </el-table-column>
        <el-table-column label="最新净值" width="110" align="right">
          <template #default="{ row }">
            <span v-if="row.nav != null">{{ row.nav.toFixed(4) }}</span>
            <el-tag v-else type="warning" size="small" effect="plain">净值缺失</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="市值" width="120" align="right">
          <template #default="{ row }">{{ formatMoney(row.market_value) }}</template>
        </el-table-column>
        <el-table-column label="浮动盈亏" width="130" align="right">
          <template #default="{ row }">
            <span :class="pnlClass(row.unrealized_pnl)">{{ formatSignedMoney(row.unrealized_pnl) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="收益率" width="100" align="right">
          <template #default="{ row }">
            <span :class="pnlClass(row.return_rate)">{{ formatPercent(row.return_rate) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="占比" width="90" align="right">
          <template #default="{ row }">{{ formatShare(row.weight) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="110" align="center">
          <template #default="{ row }">
            <el-tooltip
              :disabled="row.pricing_status === 'priced'"
              content="缺少可用净值，暂不可赎回"
              placement="top"
            >
              <span>
                <el-button
                  size="small"
                  :icon="Sell"
                  :disabled="row.pricing_status !== 'priced'"
                  @click="openSell(row)"
                >
                  卖出
                </el-button>
              </span>
            </el-tooltip>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <!-- 卖出弹窗 -->
    <el-dialog v-model="sellVisible" title="赎回确认" width="440px">
      <div v-if="target" class="sell-form">
        <p class="sell-title">
          <strong>{{ target.product_name || target.product_code }}</strong>
          <span class="mono">{{ target.product_code }}</span>
        </p>
        <dl class="sell-meta">
          <div><dt>持有份额</dt><dd>{{ formatShares(target.shares) }} 份</dd></div>
          <div><dt>最新净值</dt><dd>{{ target.nav?.toFixed(4) ?? '—' }}</dd></div>
          <div><dt>持仓成本</dt><dd>{{ formatMoney(target.cost_amount) }} 元</dd></div>
          <div><dt>浮动盈亏</dt>
            <dd :class="pnlClass(target.unrealized_pnl)">
              {{ formatSignedMoney(target.unrealized_pnl) }} 元（{{ formatPercent(target.return_rate) }}）
            </dd>
          </div>
        </dl>

        <el-form label-width="90px" label-position="left">
          <el-form-item label="全部赎回">
            <el-switch v-model="sellAll" />
          </el-form-item>
          <el-form-item v-if="!sellAll" label="赎回份额">
            <el-input-number
              v-model="sellShares"
              :min="0.01"
              :max="target.shares"
              :step="100"
              :precision="2"
              style="width: 100%"
            />
          </el-form-item>
        </el-form>
      </div>
      <template #footer>
        <el-button @click="sellVisible = false">取消</el-button>
        <el-button type="primary" :loading="selling" @click="confirmSell">确认赎回</el-button>
      </template>
    </el-dialog>
  </section>
</template>

<style scoped>
/* 页面骨架 / 指标卡 / 表格容器 / 盈亏配色等通用样式已提升到全局 style.css，
   这里只保留本页特有的微调，避免与全局规则重复。 */
.table-card :deep(.el-table__body-wrapper) { min-height: 48px; }

.product-cell { display: flex; flex-direction: column; gap: 2px; }
.product-cell small { color: var(--color-text-muted); font-size: 11px; }
.mono { color: var(--color-text-muted); font: 11px/1 var(--font-mono); }

.sell-form { display: flex; flex-direction: column; gap: 12px; }
.sell-title { margin: 0; display: flex; align-items: baseline; gap: 8px; }
.sell-meta { margin: 0; display: grid; grid-template-columns: 1fr 1fr; gap: 8px 16px; }
.sell-meta div { display: flex; flex-direction: column; gap: 2px; }
.sell-meta dt { color: var(--color-text-muted); font-size: 11px; }
.sell-meta dd { margin: 0; font-size: 13px; font-variant-numeric: tabular-nums; }

/* 持仓结构：纯 SVG 圆环（周长按 100 计，首段从正上方起算） */
.allocation {
  display: flex;
  flex-direction: column;
  gap: 12px;
  margin-bottom: 20px;
  padding: 16px 18px;
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  background: var(--color-surface);
  box-shadow: var(--shadow-sm);
}
.allocation-head h3 {
  margin: 0;
  font-family: var(--font-display);
  font-size: var(--text-lg);
  font-weight: 600;
  color: var(--color-primary);
}
.allocation-body { display: flex; align-items: center; gap: 28px; flex-wrap: wrap; }
.donut { position: relative; width: 168px; margin: 0; flex-shrink: 0; }
.donut svg { display: block; width: 100%; }
.donut-track { fill: none; stroke: var(--color-surface-alt); stroke-width: 5.2; }
.donut-seg { fill: none; stroke-width: 5.2; transition: stroke-dasharray 0.5s ease; }
.donut figcaption {
  position: absolute;
  inset: 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 2px;
  text-align: center;
}
.donut figcaption strong {
  font-family: var(--font-mono);
  font-variant-numeric: tabular-nums;
  font-size: 14px;
  color: var(--color-primary);
}
.donut figcaption span { font-size: var(--text-xs); color: var(--color-text-muted); }

.legend {
  flex: 1;
  min-width: 200px;
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
  gap: 6px 18px;
  margin: 0;
  padding: 0;
  list-style: none;
}
.legend li { display: flex; align-items: center; gap: 8px; font-size: var(--text-sm); }
.legend i { width: 9px; height: 9px; border-radius: 2px; flex-shrink: 0; }
.legend-name {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--color-text-secondary);
}
.legend b {
  font-family: var(--font-mono);
  font-variant-numeric: tabular-nums;
  color: var(--color-text);
}

@media (max-width: 980px) {
  .stat-grid { grid-template-columns: repeat(2, 1fr); }
}
@media (max-width: 768px) {
  .page-head { flex-direction: column; }
  .page-tools { width: 100%; }
}
</style>
