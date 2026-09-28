// ── 用户认证相关类型 ─────────────────────────────────────────

/** 注册请求 */
export interface RegisterRequest {
  username: string
  password: string
  display_name?: string
}

/** 登录请求 */
export interface LoginRequest {
  username: string
  password: string
}

/** 已登录用户信息（持久化到 localStorage） */
export interface UserInfo {
  customer_id: string
  username: string
  display_name: string
  token: string
  expires_in?: number
  login_at?: number
  is_admin?: boolean
}

/** 登录响应 */
export interface LoginResponse {
  customer_id: string
  username: string
  display_name: string
  token: string
  expires_in: number
  is_admin?: boolean
}

/** 注册响应 */
export interface RegisterResponse {
  customer_id: string
  username: string
  display_name: string
}
