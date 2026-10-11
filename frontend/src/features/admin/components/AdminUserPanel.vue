<script setup lang="ts">
/**
 * 管理后台：全站用户与持仓总览。
 *
 * 账户数字由后端 PortfolioService 计算，与用户自己在"账户"页面看到的同源，
 * 因此管理端不会出现第二个口径的盈亏。
 */
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { adminErrorMessage, getUserPortfolio, listUsers } from '@/features/admin/api'
import type { AdminUserEntry } from '@/features/admin/types'
import type { PositionView } from '@/features/portfolio/types'
import { formatMoney, formatPercent, formatShares, pnlClass } from '@/shared/format'

const users = ref<AdminUserEntry[]>([])
const loading = ref(false)
const authorized = ref(false)
const keyword = ref('')

const detailVisible = ref(false)
const detailLoading = ref(false)
const detailUser = ref<AdminUserEntry | null>(null)
const detailPositions = ref<PositionView[]>([])

const filtered = computed(() => {
  const text = keyword.value.trim().toLowerCase()
  if (!text) return users.value
  return users.value.filter(
    item => item.customer_id.toLowerCase().includes(text)
      || item.username.toLowerCase().includes(text)
      || item.display_name.toLowerCase().includes(text),
  )
})

const totals = computed(() => {
  const rows = users.value
  return {
    users: rows.length,
    admins: rows.filter(item => item.is_admin).length,
    assets: rows.reduce((sum, item) => sum + (item.account?.total_assets ?? 0), 0),
    withPositions: rows.filter(item => (item.account?.position_count ?? 0) > 0).length,
  }
})

async function load() {
  loading.value = true
  try {
    users.value = (await listUsers()).users
    authorized.value = true
  } catch (error: any) {
    // 403 代表当前用户不是管理员，不渲染任何管理入口。
    if (error?.response?.status !== 403) ElMessage.error(adminErrorMessage(error, '用户列表加载失败'))
    authorized.value = false
    users.value = []
  } finally {
    loading.value = false
  }
}

async function openDetail(row: AdminUserEntry) {
  detailVisible.value = true
  detailLoading.value = true
  detailUser.value = row
  detailPositions.value = []
  try {
    const payload = await getUserPortfolio(row.customer_id)
    detailUser.value = payload.user ?? row
    detailPositions.value = payload.positions
  } catch (error: any) {
    ElMessage.error(adminErrorMessage(error, '持仓明细加载失败'))
  } finally {
    detailLoading.value = false
  }
}

function dateLabel(value: string): string {
  if (!value) return '—'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleDateString('zh-CN')
}

onMounted(load)
</script>

<template>
  <el-card v-if="authorized" class="admin-users" shadow="never" v-loading="loading">
    <div class="panel-heading">
      <div>
        <p class="eyebrow">管理员运维</p>
        <h2>用户与持仓总览</h2>
      </div>
      <span class="count-badge">{{ totals.users }}</span>
    </div>
    <p class="panel-note">
      账户口径与用户端一致（同由模拟交易服务计算）。管理员 {{ totals.admins }} 个，
      有持仓 {{ totals.withPositions }} 个，全站总资产 {{ formatMoney(totals.assets) }} 元。
    </p>

    <el-input
      v-model="keyword"
      class="user-filter"
      placeholder="按 customer_id / 用户名 / 显示名筛选"
      aria-label="筛选用户"
      clearable
    />

    <el-empty v-if="!loading && !filtered.length" :image-size="54" description="没有匹配的用户" />

    <article v-for="row in filtered" :key="row.customer_id" class="user-row" @click="openDetail(row)">
      <div class="user-main">
        <div class="user-title">
          <strong>{{ row.display_name || row.username }}</strong>
          <span class="user-name">{{ row.username }}</span>
          <el-tag v-if="row.is_admin" type="success" size="small" effect="plain">管理员</el-tag>
        </div>
        <p class="user-meta">
          {{ row.customer_id }} · 注册于 {{ dateLabel(row.created_at) }}
        </p>
      </div>
      <div class="user-numbers">
        <div class="metric">
          <span class="metric-label">总资产</span>
          <span class="metric-value">{{ formatMoney(row.account?.total_assets) }}</span>
        </div>
        <div class="metric">
          <span class="metric-label">持仓</span>
          <span class="metric-value">{{ row.account?.position_count ?? 0 }} 项</span>
        </div>
        <div class="metric">
          <span class="metric-label">累计盈亏</span>
          <span class="metric-value" :class="pnlClass(row.account?.total_pnl)">
            {{ formatMoney(row.account?.total_pnl) }}
          </span>
        </div>
      </div>
    </article>

    <el-dialog
      v-model="detailVisible"
      :title="`持仓明细 · ${detailUser?.display_name || detailUser?.username || ''}`"
      width="min(680px, 94vw)"
      append-to-body
    >
      <div v-loading="detailLoading" class="detail-body">
        <div v-if="detailUser?.account" class="detail-summary">
          <div class="metric">
            <span class="metric-label">总资产</span>
            <span class="metric-value">{{ formatMoney(detailUser.account.total_assets) }}</span>
          </div>
          <div class="metric">
            <span class="metric-label">可用资金</span>
            <span class="metric-value">{{ formatMoney(detailUser.account.cash_balance) }}</span>
          </div>
          <div class="metric">
            <span class="metric-label">持仓市值</span>
            <span class="metric-value">{{ formatMoney(detailUser.account.market_value) }}</span>
          </div>
          <div class="metric">
            <span class="metric-label">累计盈亏</span>
            <span class="metric-value" :class="pnlClass(detailUser.account.total_pnl)">
              {{ formatMoney(detailUser.account.total_pnl) }}
              （{{ formatPercent(detailUser.account.total_pnl_pct) }}）
            </span>
          </div>
        </div>
        <!-- 市值不完整时显式提示：不把缺失净值的持仓静默算小。 -->
        <el-alert
          v-if="detailUser?.account && !detailUser.account.market_value_complete"
          type="warning"
          :closable="false"
          show-icon
          title="部分持仓缺少可用净值，市值与盈亏未覆盖全部持仓"
          :description="detailUser.account.pricing_issues.join('、')"
        />

        <el-empty v-if="!detailLoading && !detailPositions.length" :image-size="54" description="该用户没有持仓" />
        <el-table v-else :data="detailPositions" size="small" class="position-table">
          <el-table-column prop="product_code" label="代码" width="92" />
          <el-table-column prop="product_name" label="名称" min-width="150" show-overflow-tooltip />
          <el-table-column label="份额" width="110" align="right">
            <template #default="{ row }">{{ formatShares(row.shares) }}</template>
          </el-table-column>
          <el-table-column label="市值" width="120" align="right">
            <template #default="{ row }">{{ formatMoney(row.market_value) }}</template>
          </el-table-column>
          <el-table-column label="浮动盈亏" width="150" align="right">
            <template #default="{ row }">
              <span :class="pnlClass(row.unrealized_pnl)">
                {{ formatMoney(row.unrealized_pnl) }}（{{ formatPercent(row.return_rate) }}）
              </span>
            </template>
          </el-table-column>
        </el-table>
      </div>
    </el-dialog>
  </el-card>
</template>

<style scoped>
/* .eyebrow 用全局定义，这里不再重复声明，避免样式分叉。 */
.admin-users { border: 1px solid var(--color-border); box-shadow: var(--shadow-sm); }
.panel-heading { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; }
h2 {
  margin: 0;
  font-family: var(--font-display);
  font-size: var(--text-xl);
  font-weight: 600;
  color: var(--color-primary);
}
.count-badge {
  display: grid;
  place-items: center;
  min-width: 30px;
  height: 30px;
  padding: 0 9px;
  border-radius: 999px;
  background: var(--color-primary-soft);
  color: var(--color-primary);
  font: 12px/1 var(--font-mono);
}
.panel-note { margin: 10px 0 14px; color: var(--color-text-secondary); font-size: 12px; line-height: 1.6; }
.user-filter { margin-bottom: 12px; }
.user-row {
  display: flex;
  align-items: center;
  gap: 12px;
  border-top: 1px solid var(--color-hairline);
  padding: 12px 8px;
  cursor: pointer;
  transition: background 0.16s ease;
}
.user-row:hover { background: var(--color-primary-soft); }
.user-main { min-width: 0; flex: 1; }
.user-title { display: flex; align-items: baseline; gap: 8px; flex-wrap: wrap; color: var(--color-text); }
.user-name { color: var(--color-text-secondary); font: 12px/1 var(--font-mono); }
.user-meta { margin: 5px 0 0; color: var(--color-text-secondary); font-size: 12px; }
.user-numbers { display: flex; gap: 18px; flex-shrink: 0; }
.metric { display: flex; flex-direction: column; gap: 3px; min-width: 76px; }
.metric-label { color: var(--color-text-muted); font-size: 11px; }
.metric-value { color: var(--color-text); font-family: var(--font-mono); font-size: 12px; font-variant-numeric: tabular-nums; }
.detail-body { display: flex; flex-direction: column; gap: 14px; }
.detail-summary { display: flex; gap: 22px; flex-wrap: wrap; }
.position-table { width: 100%; }

@media (max-width: 640px) {
  .user-row { flex-direction: column; align-items: flex-start; gap: 8px; }
  .user-numbers { gap: 14px; }
}
</style>
