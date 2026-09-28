import type { AccountSnapshot, PositionView } from '../portfolio/types'
import type { ProductView } from '../product/types'

// ── 管理后台 ──────────────────────────────────────────────────

/** 管理端用户列表的一行：账号信息 + 账户概览。 */
export interface AdminUserEntry {
  customer_id: string
  username: string
  display_name: string
  created_at: string
  is_admin: boolean
  /** 账户口径与用户端同源；读取失败时为 null，不伪造 0。 */
  account: AccountSnapshot | null
}

export interface AdminUserListResponse {
  users: AdminUserEntry[]
  simulated: boolean
}

export interface AdminUserPortfolioResponse {
  customer_id: string
  user: AdminUserEntry | null
  account: AccountSnapshot | null
  positions: PositionView[]
  simulated: boolean
}

export interface AdminProductListResponse {
  products: ProductView[]
  simulated: boolean
}

/** 新增/编辑商品请求；上下架状态不在此表达。 */
export interface AdminProductUpsertRequest {
  code: string
  name: string
  type?: string
  risk_level?: string
  company?: string
  manager?: string
  scale?: number | null
  subscription_fee?: number | null
  redemption_fee?: string
  recommended_holding_period?: string
  investment_target?: string
  investment_strategy?: string
  /** 最新净值；必须为正，缺失则不写业绩区块。 */
  nav?: number | null
  /** 净值日期，对应后端 product_performance.update_date。 */
  nav_date?: string
  /** 收益率/回撤是小数（0.126 表示 12.6%），与产品库存储口径一致。 */
  return_1y?: number | null
  max_drawdown?: number | null
  volatility?: number | null
  sharpe_ratio?: number | null
}
