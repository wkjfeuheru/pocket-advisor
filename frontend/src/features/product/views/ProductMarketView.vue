<script setup lang="ts">
/** 商品货架：按基金、ETF、理财产品展示，并按金额申购。 */
import { ref, computed, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { Refresh, ShoppingCart } from '@element-plus/icons-vue'
import { listProducts } from '@/features/product/api'
import { createOrder } from '@/features/portfolio/api'
import { PRODUCT_TYPES, productTypeLabel, type ProductView } from '@/features/product/types'
import type { UserInfo } from '@/features/auth/types'
import {
  describeLimitation, formatMoney, formatRatioPercent, makeIdempotencyKey,
} from '@/shared/format'

defineProps<{ currentUser: UserInfo }>()

const loading = ref(false)
const loadError = ref('')
const products = ref<ProductView[]>([])
const keyword = ref('')
const productType = ref<(typeof PRODUCT_TYPES)[number]['value']>('fund')

const buyVisible = ref(false)
const buying = ref(false)
const target = ref<ProductView | null>(null)
const buyAmount = ref<number>(1000)
/** 每笔申购的幂等键：确认前生成一次，失败重试复用同一个键。 */
const buyKey = ref('')

const filtered = computed(() => {
  const text = keyword.value.trim().toLowerCase()
  if (!text) return products.value
  return products.value.filter(
    item => item.code.toLowerCase().includes(text) || item.name.toLowerCase().includes(text),
  )
})

async function load() {
  loading.value = true
  loadError.value = ''
  try {
    products.value = (await listProducts(productType.value)).products
  } catch (error: any) {
    products.value = []
    loadError.value = error?.message || '商品加载失败'
    ElMessage.error(loadError.value)
  } finally {
    loading.value = false
  }
}

function openBuy(item: ProductView) {
  target.value = item
  buyAmount.value = 1000
  buyKey.value = makeIdempotencyKey('buy')
  buyVisible.value = true
}

/** 前端预估份额（外扣法）：仅作参考，实际以服务端成交为准。 */
const estimatedShares = computed(() => {
  const nav = target.value?.nav.value
  if (!nav || nav <= 0 || !buyAmount.value) return null
  const rate = target.value?.subscription_fee != null
    ? target.value.subscription_fee / 100
    : 0
  const net = buyAmount.value / (1 + rate)
  return Math.floor((net / nav) * 100) / 100
})

async function confirmBuy() {
  if (!target.value) return
  if (!buyAmount.value || buyAmount.value <= 0) {
    ElMessage.warning('请输入大于 0 的申购金额')
    return
  }
  buying.value = true
  try {
    const result = await createOrder({
      product_code: target.value.code,
      side: 'buy',
      amount: buyAmount.value,
      idempotency_key: buyKey.value,
    })
    const limitations = result.order.fee_limitations
    ElMessage.success(
      `申购成功：${target.value.name} ${result.order.shares} 份，扣款 ${formatMoney(result.order.net_amount)} 元`,
    )
    if (limitations.length) {
      ElMessage.warning(limitations.map(describeLimitation).join('；'))
    }
    buyVisible.value = false
    await load()
  } catch (error: any) {
    ElMessage.error(error?.message || '申购失败')
  } finally {
    buying.value = false
  }
}

onMounted(load)
</script>

<template>
  <section class="page">
    <header class="page-head">
      <div>
        <p class="eyebrow">MARKET / PRODUCTS</p>
        <h2>商品货架</h2>
        <p class="page-note">模拟交易：按最新净值成交，申购费率按产品披露值计算。不构成投资建议。</p>
      </div>
      <div class="page-tools">
        <el-radio-group v-model="productType" @change="() => load()">
          <el-radio-button
            v-for="item in PRODUCT_TYPES"
            :key="item.value"
            :value="item.value"
          >
            {{ item.label }}
          </el-radio-button>
        </el-radio-group>
        <el-input
          v-model="keyword"
          placeholder="搜索名称或代码"
          clearable
          style="width: 220px"
        />
        <el-button :icon="Refresh" :loading="loading" @click="load">刷新</el-button>
      </div>
    </header>

    <el-card shadow="never" v-loading="loading" class="table-card">
      <el-empty v-if="!loading && loadError" :image-size="60" :description="loadError" />
      <el-empty v-else-if="!loading && !filtered.length" :image-size="60" description="暂无商品" />
      <el-table v-else :data="filtered" stripe style="width: 100%">
        <el-table-column prop="code" label="代码" width="100" />
        <el-table-column prop="name" label="名称" min-width="180" show-overflow-tooltip />
        <el-table-column label="类型" width="100">
          <template #default="{ row }">{{ productTypeLabel(row.type) }}</template>
        </el-table-column>
        <el-table-column prop="risk_level" label="风险等级" width="110" />
        <el-table-column label="最新净值" width="110" align="right">
          <template #default="{ row }">
            {{ row.nav.value != null ? row.nav.value.toFixed(4) : '—' }}
          </template>
        </el-table-column>
        <el-table-column label="近一年" width="100" align="right">
          <template #default="{ row }">{{ formatRatioPercent(row.return_1y) }}</template>
        </el-table-column>
        <el-table-column label="申购费率" width="100" align="right">
          <template #default="{ row }">
            {{ row.subscription_fee != null ? `${row.subscription_fee}%` : '未披露' }}
          </template>
        </el-table-column>
        <el-table-column label="赎回费率" width="100" align="right">
          <template #default="{ row }">
            {{ row.redemption_fee || '未披露' }}
          </template>
        </el-table-column>
        <el-table-column label="操作" width="120" align="center">
          <template #default="{ row }">
            <el-tooltip
              :disabled="row.tradable"
              content="缺少可用净值，暂不可申购"
              placement="top"
            >
              <span>
                <el-button
                  type="primary"
                  size="small"
                  :icon="ShoppingCart"
                  :disabled="!row.tradable"
                  @click="openBuy(row)"
                >
                  申购
                </el-button>
              </span>
            </el-tooltip>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <!-- 申购弹窗 -->
    <el-dialog v-model="buyVisible" title="申购确认" width="440px">
      <div v-if="target" class="buy-form">
        <p class="buy-title">
          <strong>{{ target.name }}</strong>
          <span class="mono">{{ target.code }}</span>
        </p>
        <dl class="buy-meta">
          <div><dt>最新净值</dt><dd>{{ target.nav.value?.toFixed(4) ?? '—' }}
            <small v-if="target.nav.as_of">（{{ target.nav.as_of }}）</small>
          </dd></div>
          <div><dt>申购费率</dt><dd>
            {{ target.subscription_fee != null ? `${target.subscription_fee}%` : '未披露（按 0 计算）' }}
          </dd></div>
          <div><dt>风险等级</dt><dd>{{ target.risk_level || '—' }}</dd></div>
        </dl>

        <el-form label-width="90px" label-position="left">
          <el-form-item label="申购金额">
            <el-input-number
              v-model="buyAmount"
              :min="100"
              :step="100"
              :precision="2"
              style="width: 100%"
            />
          </el-form-item>
        </el-form>

        <p class="buy-estimate">
          预估份额 <strong>{{ estimatedShares ?? '—' }}</strong> 份
          <small>（按外扣法估算，实际以服务端成交为准）</small>
        </p>
      </div>
      <template #footer>
        <el-button @click="buyVisible = false">取消</el-button>
        <el-button type="primary" :loading="buying" @click="confirmBuy">确认申购</el-button>
      </template>
    </el-dialog>
  </section>
</template>

<style scoped>
/* 页面骨架 / 表格容器等通用样式已提升到全局 style.css。 */
.table-card :deep(.el-table__body-wrapper) { min-height: 48px; }

.buy-form { display: flex; flex-direction: column; gap: 12px; }
.buy-title { margin: 0; display: flex; align-items: baseline; gap: 8px; }
.mono { color: var(--color-text-muted); font: 12px/1 var(--font-mono); }
.buy-meta { margin: 0; display: grid; grid-template-columns: 1fr 1fr; gap: 8px 16px; }
.buy-meta div { display: flex; flex-direction: column; gap: 2px; }
.buy-meta dt { color: var(--color-text-muted); font-size: 11px; }
.buy-meta dd { margin: 0; font-size: 13px; font-variant-numeric: tabular-nums; }
.buy-meta small { color: var(--color-text-muted); }
.buy-estimate { margin: 0; padding: 10px 12px; border-radius: var(--radius); background: var(--color-primary-soft); font-size: 13px; }
.buy-estimate strong { color: var(--color-primary); font-variant-numeric: tabular-nums; }
.buy-estimate small { color: var(--color-text-secondary); font-size: 11px; }

@media (max-width: 768px) {
  .page-head { flex-direction: column; }
  .page-tools { width: 100%; }
}
</style>
