// ── 商品货架只读视图（与后端 product.schemas 对齐）────

/** 带来源与截止日的数值 */
export interface SourcedValue {
  value: number | null
  source: string
  as_of: string
}

/** 商品货架上的一项 */
export interface ProductView {
  code: string
  name: string
  type: string
  risk_level: string
  company: string
  manager: string
  scale: number | null
  nav: SourcedValue
  return_1y: number | null
  max_drawdown: number | null
  sharpe_ratio: number | null
  subscription_fee: number | null
  redemption_fee: string
  recommended_holding_period: string
  investment_target: string
  /** 上架状态；false 时不可申购，但既有持仓仍可赎回。 */
  is_active: boolean
  /** 是否有可用净值且已上架；为 false 时不可交易。 */
  tradable: boolean
  limitations: string[]
}

export interface ProductShelfResponse {
  products: ProductView[]
  simulated: boolean
}

/** 货架类型。提交值是 type 列，展示用中文。 */
export const PRODUCT_TYPES = [
  { value: 'fund', label: '基金' },
  { value: 'etf', label: 'ETF' },
  { value: 'wealth', label: '理财产品' },
] as const

export function productTypeLabel(type: string): string {
  return PRODUCT_TYPES.find((item) => item.value === type)?.label ?? type
}
