/** 统一 HTTP 客户端。
 *
 * 这是前端**唯一**的 axios 实例：baseURL 固定 `/api`，请求拦截器统一注入
 * `Authorization: Bearer <token>`。各功能模块（portfolio / admin / auth）只在此基础上
 * 拼路径，不再各自 `axios.create`，避免鉴权头、超时与错误口径三处漂移。
 */

import axios from 'axios'
import { clearUser, getToken } from '@/app/session'
import { apiErrorMessage } from '@/api/errors'

// ── 唯一 axios 实例 ────────────────────────────────────────────
export const http = axios.create({
  baseURL: '/api',
  timeout: 60000,
  headers: { 'Content-Type': 'application/json' },
})

http.interceptors.request.use((config) => {
  const token = getToken()
  if (token) {
    config.headers.Authorization = `Bearer ${token}`
  }
  return config
})

// 后端把业务失败写成 detail；统一把它提升到 message，同时保留原始 response，
// 便于需要读取 state（如 403 隐藏入口）的调用方继续使用。
http.interceptors.response.use(
  (response) => response,
  (error) => {
    if (error?.response?.status === 401 && typeof window !== 'undefined') {
      clearUser()
      window.dispatchEvent(new CustomEvent('auth:expired'))
    }
    error.message = apiErrorMessage(error, error?.message || '请求失败')
    return Promise.reject(error)
  },
)

export default { http }
