/** 模拟交易 API 封装：账户、持仓、下单、清仓、记录。 */

import { http } from '@/api/client'
import type {
  AccountSnapshot,
  DepositRequest,
  DepositResponse,
  LiquidationResponse,
  OrderListResponse,
  OrderRequest,
  PositionListResponse,
  TradeResponse,
  TransactionListResponse,
} from '@/features/portfolio/types'

const PORTFOLIO_BASE = '/portfolio'

/** 账户数据面板 */
export async function getAccount(): Promise<AccountSnapshot> {
  const { data } = await http.get<AccountSnapshot>(`${PORTFOLIO_BASE}/account`)
  return data
}

/** 持仓明细 */
export async function listPositions(): Promise<PositionListResponse> {
  const { data } = await http.get<PositionListResponse>(`${PORTFOLIO_BASE}/positions`)
  return data
}

/** 充值 */
export async function deposit(payload: DepositRequest): Promise<DepositResponse> {
  const { data } = await http.post<DepositResponse>(`${PORTFOLIO_BASE}/deposit`, payload)
  return data
}

/** 下单（买入/卖出） */
export async function createOrder(payload: OrderRequest): Promise<TradeResponse> {
  const { data } = await http.post<TradeResponse>(`${PORTFOLIO_BASE}/orders`, payload)
  return data
}

/** 一键清仓 */
export async function liquidate(): Promise<LiquidationResponse> {
  const { data } = await http.post<LiquidationResponse>(`${PORTFOLIO_BASE}/liquidate`, {})
  return data
}

/** 成交记录 */
export async function listOrders(limit = 100): Promise<OrderListResponse> {
  const { data } = await http.get<OrderListResponse>(`${PORTFOLIO_BASE}/orders`, {
    params: { limit },
  })
  return data
}

/** 资金流水 */
export async function listTransactions(limit = 100): Promise<TransactionListResponse> {
  const { data } = await http.get<TransactionListResponse>(`${PORTFOLIO_BASE}/transactions`, {
    params: { limit },
  })
  return data
}

export default {
  getAccount,
  listPositions,
  deposit,
  createOrder,
  liquidate,
  listOrders,
  listTransactions,
}
