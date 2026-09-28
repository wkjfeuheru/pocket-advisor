/** 用户只读货架 API：商品列表与详情。路径仍挂在 /portfolio/products。 */

import { http } from '@/api/client'
import type { ProductShelfResponse, ProductView } from '@/features/product/types'

const PORTFOLIO_BASE = '/portfolio'

/** 商品货架 */
export async function listProducts(productType = 'fund'): Promise<ProductShelfResponse> {
  const { data } = await http.get<ProductShelfResponse>(`${PORTFOLIO_BASE}/products`, {
    params: { product_type: productType },
  })
  return data
}

/** 单品详情 */
export async function getProduct(code: string): Promise<ProductView> {
  const { data } = await http.get<ProductView>(
    `${PORTFOLIO_BASE}/products/${encodeURIComponent(code)}`,
  )
  return data
}
