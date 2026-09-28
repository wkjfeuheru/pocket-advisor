// ── 模拟交易类型（与后端 portfolio.schemas 对齐）────

/** 一笔持仓及其浮动盈亏 */
export interface PositionView {
  product_code: string
  product_name: string
  /** 产品类型与风险等级（透传自产品库），供配置诊断分档。 */
  product_type?: string
  risk_level?: string
  shares: number
  cost_amount: number
  avg_cost: number
  nav: number | null
  nav_as_of: string
  market_value: number | null
  unrealized_pnl: number | null
  return_rate: number | null
  weight: number | null
  opened_at: string | null
  updated_at: string | null
  pricing_status: 'priced' | 'unavailable'
  limitations: string[]
}

/** 一条已成交委托 */
export interface OrderView {
  order_id: string
  product_code: string
  product_name: string
  side: 'buy' | 'sell'
  shares: number
  price: number
  gross_amount: number
  fee: number
  net_amount: number
  realized_pnl: number | null
  /** 费率未披露等需要提示的限制项。 */
  fee_limitations: string[]
  created_at: string | null
}

/** 一条资金流水 */
export interface TransactionView {
  txn_id: string
  kind: 'deposit' | 'buy' | 'sell' | 'fee'
  amount: number
  balance_after: number
  ref_id: string
  note: string
  created_at: string | null
}

/** 账户数据面板口径 */
export interface AccountSnapshot {
  customer_id: string
  cash_balance: number
  frozen_balance: number
  market_value: number
  total_assets: number
  total_deposit: number
  total_pnl: number
  total_pnl_pct: number | null
  realized_pnl: number
  position_pnl: number
  position_cost: number
  position_count: number
  /** 为 false 时市值只覆盖可定价持仓，缺失项见 pricing_issues。 */
  market_value_complete: boolean
  pricing_issues: string[]
  updated_at: string | null
  simulated: boolean
  disclaimer: string
}

export interface PositionListResponse {
  customer_id: string
  positions: PositionView[]
  account: AccountSnapshot
  simulated: boolean
}

export interface OrderListResponse {
  customer_id: string
  orders: OrderView[]
  simulated: boolean
}

export interface TransactionListResponse {
  customer_id: string
  transactions: TransactionView[]
  simulated: boolean
}

export interface DepositRequest {
  amount: number
  idempotency_key?: string
}

export interface DepositResponse {
  transaction: TransactionView
  account: AccountSnapshot
  idempotent_replay: boolean
  simulated: boolean
}

export interface OrderRequest {
  product_code: string
  side: 'buy' | 'sell'
  amount?: number
  shares?: number
  all?: boolean
  idempotency_key?: string
}

export interface TradeResponse {
  order: OrderView
  account: AccountSnapshot
  position: PositionView | null
  idempotent_replay: boolean
}

export interface LiquidationResponse {
  orders: OrderView[]
  cleared_codes: string[]
  /** 无法定价等原因未清仓的商品 → 错误码。 */
  failed: Record<string, string>
  total_cash_in: number
  total_realized_pnl: number
  account: AccountSnapshot
}
