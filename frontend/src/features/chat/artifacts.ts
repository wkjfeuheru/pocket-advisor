/** 投顾公开 artifact 与澄清请求路径。 */

export type StockScenario = 'market_overview' | 'equity_research' | 'stock_screening'

export interface CandidateOption {
  id: string
  label: string
  field?: string
}

export interface ClarificationState {
  kind: 'clarification'
  reason: string
  question: string
  fields: string[]
  options: CandidateOption[]
  request?: Record<string, unknown>
}

/** 一个可采纳的持仓配置方案。 */
export interface AllocationPlan {
  plan_id: string
  tier: string
  label: string
  turnover_rate: number
  estimated_fee: number
  summary?: string
  items: AllocationPlanItem[]
  limitations?: string[]
  unavailable_reason?: string
}

export interface AllocationPlanItem {
  product_code: string
  product_name?: string
  action: string
  current_weight?: number
  target_weight?: number
  delta_weight?: number
  reason?: string
}

/** 等待用户选择方案的挂起状态。 */
export interface AllocationState {
  kind: 'allocation'
  agentVersion: string
  plans: AllocationPlan[]
}

const ACTION_LABELS: Record<string, string> = {
  open: '新开',
  add: '加仓',
  reduce: '减仓',
  close: '清仓',
  hold: '持有',
}

export function actionLabel(action: string): string {
  return ACTION_LABELS[action] || action
}

export function allocationAdoptPath(requestId: string): string {
  return `/agents/runs/${requestId}/allocation/stream`
}

/**
 * 从公开中断载荷里取方案列表。
 *
 * 与澄清一样**只信服务端声明的候选**：拿不到 ``plan_id`` 的条目一律丢弃，
 * 否则前端会渲染出一个点了也没法采纳的按钮。
 */
export function allocationPlansFrom(payload: unknown): AllocationPlan[] {
  if (!payload || typeof payload !== 'object') return []
  const raw = (payload as Record<string, any>).plans
  if (!Array.isArray(raw)) return []
  return raw
    .filter((item) => item && typeof item === 'object' && typeof item.plan_id === 'string' && item.plan_id)
    .map((item) => ({
      plan_id: String(item.plan_id),
      tier: String(item.tier || ''),
      label: String(item.label || item.tier || ''),
      turnover_rate: Number(item.turnover_rate ?? 0),
      estimated_fee: Number(item.estimated_fee ?? 0),
      summary: item.summary ? String(item.summary) : undefined,
      items: Array.isArray(item.items) ? item.items : [],
      limitations: Array.isArray(item.limitations) ? item.limitations : [],
      unavailable_reason: item.unavailable_reason ? String(item.unavailable_reason) : undefined,
    }))
}

export function isAllocationPayload(payload: unknown): payload is AllocationState {
  if (!payload || typeof payload !== 'object') return false
  const value = payload as Record<string, any>
  return value.kind === 'allocation' && Array.isArray(value.plans)
}

/** 换手率 → 展示用百分比。前端不做四舍五入以外的加工，避免"美化"数字。 */
export function formatPercent(value: number, digits = 2): string {
  if (!Number.isFinite(value)) return '—'
  return `${(value * 100).toFixed(digits)}%`
}

export function formatAmount(value: number): string {
  if (!Number.isFinite(value)) return '—'
  return value.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
}

export function publicArtifacts(input: unknown): Record<string, any>[] {
  if (!Array.isArray(input)) return []
  return input.filter((item) => item && typeof item === 'object' && !Array.isArray(item)) as Record<string, any>[]
}

export function primaryStockArtifact(input: unknown): Record<string, any> | null {
  const items = publicArtifacts(input)
  return (
    items.find((item) => item.scenario === 'market_overview'
      || item.scenario === 'equity_research'
      || item.scenario === 'stock_screening')
    || items[0]
    || null
  )
}

export function clarifyStreamPath(requestId: string): string {
  return `/agents/runs/${requestId}/clarify/stream`
}

export function optionsForField(options: CandidateOption[], field: string): CandidateOption[] {
  const tagged = options.filter((item) => item.field === field)
  if (tagged.length) return tagged
  if (options.some((item) => item.field)) return []
  return options
}

export function marketOverviewView(overview: Record<string, any> | null | undefined) {
  const sectors = overview?.sectors || {}
  const risks = [
    ...(Array.isArray(overview?.risks) ? overview.risks : []),
    ...(Array.isArray(overview?.warnings) ? overview.warnings : []),
  ].map((item) => String(item)).filter(Boolean)
  return {
    industries: Array.isArray(sectors.industry) ? sectors.industry : [],
    concepts: Array.isArray(sectors.concept) ? sectors.concept : [],
    risks,
  }
}

export function isClarificationPayload(payload: unknown): payload is ClarificationState {
  return Boolean(payload && typeof payload === 'object' && (payload as ClarificationState).kind === 'clarification')
}
