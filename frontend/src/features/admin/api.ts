/** 管理后台 API 封装：商品发行/上下架、用户与持仓总览。
 *
 * 与用户端同构：Bearer token 由拦截器注入，403 代表当前用户不是管理员。
 * 写入 DTO 只在本文件；只读货架/账户类型从用户 feature 引用，不复制口径。
 */

import { http } from '@/api/client'
import { apiErrorMessage } from '@/api/errors'
import type {
  AdminProductListResponse,
  AdminProductUpsertRequest,
  AdminUserListResponse,
  AdminUserPortfolioResponse,
} from '@/features/admin/types'
import type { ProductView } from '@/features/product/types'

/** 后端把业务失败写成 detail；面板统一用它做提示，403 用于隐藏入口。 */
export function adminErrorMessage(error: any, fallback = '请求失败'): string {
  return apiErrorMessage(error, fallback)
}

const ADMIN_BASE = '/admin'

/** 全站用户及账户概览 */
export async function listUsers(): Promise<AdminUserListResponse> {
  const { data } = await http.get<AdminUserListResponse>(`${ADMIN_BASE}/users`)
  return data
}

/** 指定用户的账户与持仓明细 */
export async function getUserPortfolio(customerId: string): Promise<AdminUserPortfolioResponse> {
  const { data } = await http.get<AdminUserPortfolioResponse>(
    `${ADMIN_BASE}/users/${encodeURIComponent(customerId)}/portfolio`,
  )
  return data
}

/** 完整货架（含已下架商品） */
export async function listAllProducts(productType = 'fund'): Promise<AdminProductListResponse> {
  const { data } = await http.get<AdminProductListResponse>(`${ADMIN_BASE}/products`, {
    params: { product_type: productType },
  })
  return data
}

/** 新增或更新商品（发行/编辑） */
export async function saveProduct(
  request: AdminProductUpsertRequest,
): Promise<{ product: ProductView }> {
  const { data } = await http.post<{ product: ProductView }>(`${ADMIN_BASE}/products`, request)
  return data
}

/** 下架商品：不可再申购，既有持仓仍可赎回 */
export async function offlineProduct(code: string): Promise<{ product: ProductView }> {
  const { data } = await http.post<{ product: ProductView }>(
    `${ADMIN_BASE}/products/${encodeURIComponent(code)}/offline`,
  )
  return data
}

/** 重新上架商品 */
export async function publishProduct(code: string): Promise<{ product: ProductView }> {
  const { data } = await http.post<{ product: ProductView }>(
    `${ADMIN_BASE}/products/${encodeURIComponent(code)}/publish`,
  )
  return data
}
