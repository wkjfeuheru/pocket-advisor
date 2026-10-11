<script setup lang="ts">
/**
 * 管理后台：商品发行与上下架。
 *
 * 下架是软状态（is_active=false）：不会删除商品，也不会影响既有持仓的赎回；
 * 只阻止新的申购。因此这里的操作按钮是"下架/上架"而不是"删除"。
 */
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { adminErrorMessage, listAllProducts, offlineProduct, publishProduct, saveProduct } from '@/features/admin/api'
import { PRODUCT_TYPES, productTypeLabel, type ProductView } from '@/features/product/types'

const products = ref<ProductView[]>([])
const loading = ref(false)
const saving = ref(false)
const authorized = ref(false)
const keyword = ref('')
const showForm = ref(false)

const blankForm = () => ({
  code: '', name: '', type: 'fund', risk_level: '', company: '', manager: '',
  // 费率/规模在表单里是字符串（el-input 的取值形态），提交时再转数字。
  scale: '', subscription_fee: '', redemption_fee: '',
  recommended_holding_period: '', investment_target: '', investment_strategy: '',
  // 业绩区块：净值是绝对值，收益率/回撤在表单里按百分比填写，提交时转成小数。
  nav: '', nav_date: '', return_1y: '', max_drawdown: '', sharpe_ratio: '',
})
const form = ref(blankForm())
const isEditing = ref(false)

/** 空串表示"未披露"，转成 null 而不是 0，避免把缺失误报成一个真实费率。 */
function toNumber(value: string): number | null {
  const text = String(value ?? '').trim()
  if (!text) return null
  const parsed = Number(text)
  return Number.isFinite(parsed) ? parsed : null
}

/** 表单按百分比填写（12.6 表示 12.6%），产品库存的是小数，这里统一换算。 */
function toRatio(value: string): number | null {
  const parsed = toNumber(value)
  return parsed === null ? null : parsed / 100
}

/** 小数转回表单里的百分比文本；null 显示为空串（未披露）。 */
function toPercentText(value: number | null): string {
  return value != null ? String(Number((value * 100).toFixed(6))) : ''
}

const filtered = computed(() => {
  const text = keyword.value.trim().toLowerCase()
  if (!text) return products.value
  return products.value.filter(
    item => item.code.toLowerCase().includes(text) || item.name.toLowerCase().includes(text),
  )
})

const offlineCount = computed(() => products.value.filter(item => !item.is_active).length)

async function load() {
  loading.value = true
  try {
    products.value = (await listAllProducts('all')).products
    authorized.value = true
  } catch (error: any) {
    // 403 代表当前用户不是管理员，不渲染任何管理入口。
    if (error?.response?.status !== 403) ElMessage.error(adminErrorMessage(error, '商品列表加载失败'))
    authorized.value = false
    products.value = []
  } finally {
    loading.value = false
  }
}

function openCreate() {
  form.value = blankForm()
  isEditing.value = false
  showForm.value = true
}

function openEdit(item: ProductView) {
  form.value = {
    code: item.code,
    name: item.name,
    type: item.type,
    risk_level: item.risk_level,
    company: item.company,
    manager: item.manager,
    scale: item.scale != null ? String(item.scale) : '',
    subscription_fee: item.subscription_fee != null ? String(item.subscription_fee) : '',
    redemption_fee: item.redemption_fee,
    recommended_holding_period: item.recommended_holding_period,
    investment_target: item.investment_target,
    investment_strategy: '',
    nav: item.nav.value != null ? String(item.nav.value) : '',
    nav_date: item.nav.as_of || '',
    return_1y: toPercentText(item.return_1y),
    max_drawdown: toPercentText(item.max_drawdown),
    sharpe_ratio: item.sharpe_ratio != null ? String(item.sharpe_ratio) : '',
  }
  isEditing.value = true
  showForm.value = true
}

/** 编辑已有商品时代码不可改：它是成交记录与持仓的外键。 */
function submit() {
  const code = form.value.code.trim()
  const name = form.value.name.trim()
  if (!code || !name) {
    ElMessage.warning('请填写商品代码与名称')
    return
  }
  ElMessageBox.confirm(
    isEditing.value
      ? `确定保存对「${name}」的修改吗？`
      : `确定发行新商品「${name}」（${code}）吗？`,
    isEditing.value ? '保存修改' : '发行商品',
    { type: 'info', confirmButtonText: '确定', cancelButtonText: '取消' },
  ).then(save).catch(() => undefined)
}

async function save() {
  saving.value = true
  try {
    const saved = await saveProduct({
      code: form.value.code.trim(),
      name: form.value.name.trim(),
      type: form.value.type,
      risk_level: form.value.risk_level,
      company: form.value.company,
      manager: form.value.manager,
      scale: toNumber(form.value.scale),
      subscription_fee: toNumber(form.value.subscription_fee),
      redemption_fee: form.value.redemption_fee,
      recommended_holding_period: form.value.recommended_holding_period,
      investment_target: form.value.investment_target,
      investment_strategy: form.value.investment_strategy,
      nav: toNumber(form.value.nav),
      nav_date: form.value.nav_date.trim(),
      return_1y: toRatio(form.value.return_1y),
      max_drawdown: toRatio(form.value.max_drawdown),
      sharpe_ratio: toNumber(form.value.sharpe_ratio),
    })
    const index = products.value.findIndex(item => item.code === saved.product.code)
    if (index >= 0) products.value[index] = saved.product
    else products.value = [saved.product, ...products.value]
    showForm.value = false
    ElMessage.success(isEditing.value ? '商品已更新' : '商品已发行')
  } catch (error: any) {
    ElMessage.error(adminErrorMessage(error, '保存失败'))
  } finally {
    saving.value = false
  }
}

async function setActive(item: ProductView, active: boolean) {
  const action = active ? '上架' : '下架'
  try {
    await ElMessageBox.confirm(
      active
        ? `重新上架「${item.name}」后，所有用户都可以申购。`
        : `下架「${item.name}」：用户将无法再申购，但既有持仓仍可赎回。`,
      `${action}商品`,
      { type: 'warning', confirmButtonText: `确定${action}`, cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  try {
    const result = active ? await publishProduct(item.code) : await offlineProduct(item.code)
    const index = products.value.findIndex(row => row.code === item.code)
    if (index >= 0) products.value[index] = result.product
    ElMessage.success(`商品已${action}`)
  } catch (error: any) {
    ElMessage.error(adminErrorMessage(error, `${action}失败`))
  }
}

onMounted(load)
</script>

<template>
  <el-card v-if="authorized" class="admin-products" shadow="never" v-loading="loading">
    <div class="panel-heading">
      <div>
        <p class="eyebrow">管理员运维</p>
        <h2>商品发行与上下架</h2>
      </div>
      <span class="count-badge">{{ products.length }}</span>
    </div>
    <p class="panel-note">
      下架为软下架：商品与历史成交都保留，既有持仓仍可赎回，只阻止新的申购。
      已下架商品 {{ offlineCount }} 个。
    </p>

    <div class="panel-toolbar">
      <el-input v-model="keyword" placeholder="按代码或名称筛选" aria-label="筛选商品" clearable />
      <el-button type="primary" @click="openCreate">发行新商品</el-button>
    </div>

    <el-empty v-if="!loading && !filtered.length" :image-size="54" description="没有匹配的商品" />

    <article v-for="item in filtered" :key="item.code" class="product-row">
      <div class="product-main">
        <div class="product-title">
          <strong>{{ item.name }}</strong>
          <span class="product-code">{{ item.code }}</span>
          <el-tag v-if="!item.is_active" type="info" size="small" effect="plain">已下架</el-tag>
        </div>
        <p class="product-meta">
          {{ productTypeLabel(item.type) }} · {{ item.risk_level || '风险等级未披露' }} ·
          净值 {{ item.nav.value ?? '未披露' }}
        </p>
      </div>
      <div class="product-actions">
        <el-button size="small" plain @click="openEdit(item)">编辑</el-button>
        <el-button
          v-if="item.is_active"
          size="small"
          type="danger"
          plain
          @click="setActive(item, false)"
        >
          下架
        </el-button>
        <el-button v-else size="small" type="success" plain @click="setActive(item, true)">
          重新上架
        </el-button>
      </div>
    </article>

    <el-dialog
      v-model="showForm"
      :title="isEditing ? '编辑商品' : '发行新商品'"
      width="min(560px, 92vw)"
      append-to-body
    >
      <div class="product-form">
        <el-input v-model="form.code" :disabled="isEditing" placeholder="商品代码（如 110011）" aria-label="商品代码" />
        <el-input v-model="form.name" placeholder="商品名称" aria-label="商品名称" />
        <el-select v-model="form.type" placeholder="商品类型" aria-label="商品类型" style="width: 100%">
          <el-option
            v-for="item in PRODUCT_TYPES"
            :key="item.value"
            :label="item.label"
            :value="item.value"
          />
        </el-select>
        <el-input v-model="form.company" placeholder="基金公司" aria-label="基金公司" />
        <el-input v-model="form.manager" placeholder="基金经理" aria-label="基金经理" />
        <el-input v-model="form.risk_level" placeholder="风险等级（如 R3 中风险）" aria-label="风险等级" />
        <el-input v-model="form.subscription_fee" placeholder="申购费率（%，如 1.5）" aria-label="申购费率" />
        <el-input v-model="form.redemption_fee" placeholder="赎回费率（如 0.5%）" aria-label="赎回费率" />
        <el-input v-model="form.recommended_holding_period" placeholder="建议持有期" aria-label="建议持有期" />
        <el-input
          v-model="form.investment_target"
          type="textarea"
          :rows="2"
          placeholder="投资目标"
          aria-label="投资目标"
        />

        <p class="form-section">业绩数据（留空则不写入；填了净值商品才能申购）</p>
        <div class="form-grid">
          <el-input v-model="form.nav" placeholder="最新净值（如 1.2345）" aria-label="最新净值" />
          <el-input v-model="form.nav_date" placeholder="净值日期（如 2026-09-08）" aria-label="净值日期" />
          <el-input v-model="form.return_1y" placeholder="近一年收益（%，如 12.6）" aria-label="近一年收益" />
          <el-input v-model="form.max_drawdown" placeholder="最大回撤（%，如 18.7）" aria-label="最大回撤" />
          <el-input v-model="form.sharpe_ratio" placeholder="夏普比率（如 0.78）" aria-label="夏普比率" />
        </div>
      </div>
      <template #footer>
        <el-button plain @click="showForm = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="submit">
          {{ isEditing ? '保存' : '发行' }}
        </el-button>
      </template>
    </el-dialog>
  </el-card>
</template>

<style scoped>
/* .eyebrow 用全局定义，这里不再重复声明，避免样式分叉。 */
.admin-products { border: 1px solid var(--color-border); box-shadow: var(--shadow-sm); }
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
.panel-toolbar { display: flex; gap: 8px; margin-bottom: 12px; }
.product-row {
  display: flex;
  align-items: center;
  gap: 12px;
  border-top: 1px solid var(--color-hairline);
  padding: 12px 8px;
  transition: background 0.16s ease;
}
.product-row:hover { background: var(--color-primary-soft); }
.product-main { min-width: 0; flex: 1; }
.product-title { display: flex; align-items: baseline; gap: 8px; flex-wrap: wrap; color: var(--color-text); }
.product-code { color: var(--color-text-secondary); font: 12px/1 var(--font-mono); }
.product-meta { margin: 5px 0 0; color: var(--color-text-secondary); font-size: 12px; }
.product-actions { display: flex; gap: 8px; flex-shrink: 0; }
.product-form { display: grid; gap: 8px; }
.form-section { margin: 6px 0 0; color: var(--color-text-secondary); font-size: 12px; }
.form-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 8px; }
</style>
