<script setup lang="ts">
/** 账户数据面板：总资产/可用资金/持仓市值/累计盈亏 + 充值 + 资金流水与成交记录。 */
import { ref, computed, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { Refresh, Wallet, Tickets } from '@element-plus/icons-vue'
import { deposit, getAccount, listOrders, listTransactions } from '@/features/portfolio/api'
import type {
  AccountSnapshot, OrderView, TransactionView,
} from '@/features/portfolio/types'
import type { UserInfo } from '@/features/auth/types'
import {
  describeLimitation, formatDateTime, formatMoney, formatPercent, formatShares,
  formatSignedMoney, makeIdempotencyKey, pnlClass,
} from '@/shared/format'

const props = defineProps<{ currentUser: UserInfo }>()

const loading = ref(false)
const loadError = ref('')
const account = ref<AccountSnapshot | null>(null)
const orders = ref<OrderView[]>([])
const transactions = ref<TransactionView[]>([])
const activeTab = ref('transactions')

const depositVisible = ref(false)
const depositing = ref(false)
const depositAmount = ref<number>(10000)
/** 每笔充值的幂等键：确认前生成一次，重试复用同一个键避免重复入账。 */
const depositKey = ref('')

const hasUnpriced = computed(() => account.value?.market_value_complete === false)

const KIND_TEXT: Record<string, string> = {
  deposit: '充值', buy: '申购', sell: '赎回', fee: '费用',
}

async function load() {
  loading.value = true
  loadError.value = ''
  try {
    const [accountData, orderData, txnData] = await Promise.all([
      getAccount(), listOrders(50), listTransactions(50),
    ])
    account.value = accountData
    orders.value = orderData.orders
    transactions.value = txnData.transactions
  } catch (error: any) {
    account.value = null
    orders.value = []
    transactions.value = []
    loadError.value = error?.message || '账户数据加载失败'
    ElMessage.error(loadError.value)
  } finally {
    loading.value = false
  }
}

function openDeposit() {
  depositAmount.value = 10000
  depositKey.value = makeIdempotencyKey('dep')
  depositVisible.value = true
}

async function confirmDeposit() {
  if (!depositAmount.value || depositAmount.value <= 0) {
    ElMessage.warning('请输入大于 0 的充值金额')
    return
  }
  depositing.value = true
  try {
    const result = await deposit({
      amount: depositAmount.value,
      idempotency_key: depositKey.value,
    })
    if (result.idempotent_replay) {
      ElMessage.info('该充值已入账，未重复扣加')
    } else {
      ElMessage.success(`充值成功，当前可用资金 ${formatMoney(result.account.cash_balance)} 元`)
    }
    depositVisible.value = false
    await load()
  } catch (error: any) {
    ElMessage.error(error?.message || '充值失败')
  } finally {
    depositing.value = false
  }
}

onMounted(load)
</script>

<template>
  <section class="page">
    <header class="page-head">
      <div>
        <p class="eyebrow">ACCOUNT / DASHBOARD</p>
        <h2>账户数据面板</h2>
        <p class="page-note">
          模拟交易账户 {{ props.currentUser?.customer_id }}；「累计盈亏」为总资产相对累计充值本金的差额，含已实现与浮动盈亏。
        </p>
      </div>
      <div class="page-tools">
        <el-button :icon="Refresh" :loading="loading" @click="load">刷新</el-button>
        <el-button type="primary" :icon="Wallet" @click="openDeposit">充值</el-button>
      </div>
    </header>

    <el-alert
      v-if="loadError"
      type="error"
      show-icon
      :closable="false"
      class="warn-alert"
      :title="loadError"
    />

    <el-alert
      v-if="hasUnpriced"
      type="warning"
      show-icon
      :closable="false"
      class="warn-alert"
      title="部分持仓缺少可用净值，市值与盈亏口径不完整"
      :description="`以下商品未计入：${(account?.pricing_issues ?? []).join('、')}`"
    />

    <div v-loading="loading">
      <div v-if="account" class="stat-grid">
        <div class="stat primary">
          <span class="stat-label">总资产</span>
          <strong>{{ formatMoney(account.total_assets, '元') }}</strong>
        </div>
        <div class="stat">
          <span class="stat-label">可用资金</span>
          <strong>{{ formatMoney(account.cash_balance, '元') }}</strong>
        </div>
        <div class="stat">
          <span class="stat-label">持仓市值</span>
          <strong>{{ formatMoney(account.market_value, '元') }}</strong>
        </div>
        <div class="stat">
          <span class="stat-label">累计盈亏</span>
          <strong :class="pnlClass(account.total_pnl)">
            {{ formatSignedMoney(account.total_pnl) }} 元
          </strong>
          <small :class="pnlClass(account.total_pnl_pct)">
            {{ formatPercent(account.total_pnl_pct) }}
          </small>
        </div>
        <div class="stat">
          <span class="stat-label">累计充值本金</span>
          <strong>{{ formatMoney(account.total_deposit, '元') }}</strong>
        </div>
        <div class="stat">
          <span class="stat-label">已实现盈亏</span>
          <strong :class="pnlClass(account.realized_pnl)">
            {{ formatSignedMoney(account.realized_pnl) }} 元
          </strong>
        </div>
        <div class="stat">
          <span class="stat-label">浮动盈亏</span>
          <strong :class="pnlClass(account.position_pnl)">
            {{ formatSignedMoney(account.position_pnl) }} 元
          </strong>
        </div>
        <div class="stat">
          <span class="stat-label">持仓数量</span>
          <strong>{{ account.position_count }} 只</strong>
        </div>
      </div>

      <el-card shadow="never" class="table-card">
        <el-tabs v-model="activeTab">
          <el-tab-pane name="transactions">
            <template #label>
              <span class="tab-label"><el-icon><Wallet /></el-icon>资金流水</span>
            </template>
            <el-empty
              v-if="!loading && !transactions.length"
              :image-size="50"
              description="暂无资金流水，先充值开始"
            />
            <el-table v-else :data="transactions" stripe style="width: 100%">
              <el-table-column label="时间" width="160">
                <template #default="{ row }">{{ formatDateTime(row.created_at) }}</template>
              </el-table-column>
              <el-table-column label="类型" width="90">
                <template #default="{ row }">{{ KIND_TEXT[row.kind] || row.kind }}</template>
              </el-table-column>
              <el-table-column label="变动金额" width="140" align="right">
                <template #default="{ row }">
                  <span :class="pnlClass(row.amount)">{{ formatSignedMoney(row.amount) }}</span>
                </template>
              </el-table-column>
              <el-table-column label="余额" width="140" align="right">
                <template #default="{ row }">{{ formatMoney(row.balance_after) }}</template>
              </el-table-column>
              <el-table-column prop="note" label="备注" min-width="160" show-overflow-tooltip />
            </el-table>
          </el-tab-pane>

          <el-tab-pane name="orders">
            <template #label>
              <span class="tab-label"><el-icon><Tickets /></el-icon>成交记录</span>
            </template>
            <el-empty
              v-if="!loading && !orders.length"
              :image-size="50"
              description="暂无成交记录"
            />
            <el-table v-else :data="orders" stripe style="width: 100%">
              <el-table-column label="时间" width="160">
                <template #default="{ row }">{{ formatDateTime(row.created_at) }}</template>
              </el-table-column>
              <el-table-column label="方向" width="80">
                <template #default="{ row }">
                  <el-tag :type="row.side === 'buy' ? 'danger' : 'success'" size="small" effect="plain">
                    {{ row.side === 'buy' ? '申购' : '赎回' }}
                  </el-tag>
                </template>
              </el-table-column>
              <el-table-column label="商品" min-width="170" show-overflow-tooltip>
                <template #default="{ row }">
                  {{ row.product_name || row.product_code }}
                  <small class="mono">{{ row.product_code }}</small>
                </template>
              </el-table-column>
              <el-table-column label="份额" width="110" align="right">
                <template #default="{ row }">{{ formatShares(row.shares) }}</template>
              </el-table-column>
              <el-table-column label="成交价" width="100" align="right">
                <template #default="{ row }">{{ row.price.toFixed(4) }}</template>
              </el-table-column>
              <el-table-column label="费用" width="100" align="right">
                <template #default="{ row }">
                  <el-tooltip
                    :disabled="!row.fee_limitations.length"
                    :content="row.fee_limitations.map(describeLimitation).join('；')"
                    placement="top"
                  >
                    <span>
                      {{ formatMoney(row.fee) }}
                      <el-icon v-if="row.fee_limitations.length" class="fee-warn">!</el-icon>
                    </span>
                  </el-tooltip>
                </template>
              </el-table-column>
              <el-table-column label="已实现盈亏" width="130" align="right">
                <template #default="{ row }">
                  <span :class="pnlClass(row.realized_pnl)">{{ formatSignedMoney(row.realized_pnl) }}</span>
                </template>
              </el-table-column>
            </el-table>
          </el-tab-pane>
        </el-tabs>
      </el-card>

      <p v-if="account" class="disclaimer">{{ account.disclaimer }}</p>
    </div>

    <!-- 充值弹窗 -->
    <el-dialog v-model="depositVisible" title="账户充值" width="420px">
      <div class="deposit-form">
        <p class="deposit-note">
          为模拟账户注入虚拟资金，用于体验申购与持仓功能。单笔上限 1,000 万元。
        </p>
        <el-form label-width="90px" label-position="left">
          <el-form-item label="充值金额">
            <el-input-number
              v-model="depositAmount"
              :min="1"
              :step="1000"
              :precision="2"
              style="width: 100%"
            />
          </el-form-item>
        </el-form>
        <div class="quick-row">
          <el-button
            v-for="amount in [10000, 50000, 100000, 500000]"
            :key="amount"
            size="small"
            text
            @click="depositAmount = amount"
          >
            {{ formatMoney(amount) }}
          </el-button>
        </div>
      </div>
      <template #footer>
        <el-button @click="depositVisible = false">取消</el-button>
        <el-button type="primary" :loading="depositing" @click="confirmDeposit">确认充值</el-button>
      </template>
    </el-dialog>
  </section>
</template>

<style scoped>
/* 页面骨架 / 指标卡 / 表格容器 / 盈亏配色等通用样式已提升到全局 style.css。 */
.table-card :deep(.el-table__body-wrapper) { min-height: 48px; }

.tab-label { display: inline-flex; align-items: center; gap: 6px; }
.mono { color: var(--color-text-muted); font: 11px/1 var(--font-mono); margin-left: 6px; }
.fee-warn { color: var(--color-danger); margin-left: 4px; }

.disclaimer { margin: 12px 0 0; color: var(--color-text-muted); font-size: 11px; }

.deposit-form { display: flex; flex-direction: column; gap: 8px; }
.deposit-note { margin: 0; color: var(--color-text-secondary); font-size: 12px; }
.quick-row { display: flex; align-items: center; gap: 4px; flex-wrap: wrap; }

@media (max-width: 980px) {
  .stat-grid { grid-template-columns: repeat(2, 1fr); }
}
@media (max-width: 768px) {
  .page-head { flex-direction: column; }
  .page-tools { width: 100%; }
}
</style>
