/** 路由：用户侧（商品 / 持仓 / 账户）与管理员侧（管理后台）分离。
 *
 * 管理员与普通用户看到的是两套互不重叠的界面：管理员只会进入 /admin，
 * 普通用户只能看到用户侧页面。真正的鉴权仍由后端 token + 角色校验兜底，
 * 这里的跳转只是为了不把对方的界面渲染出来。
 */

import { createRouter, createWebHistory } from 'vue-router'
import { getStoredUser } from '@/app/session'

const routes = [
  { path: '/', redirect: '/market' },
  {
    path: '/market',
    name: 'market',
    component: () => import('@/features/product/views/ProductMarketView.vue'),
    meta: { title: '商品' },
  },
  {
    path: '/positions',
    name: 'positions',
    component: () => import('@/features/portfolio/views/PositionsView.vue'),
    meta: { title: '持仓' },
  },
  {
    path: '/account',
    name: 'account',
    component: () => import('@/features/portfolio/views/AccountView.vue'),
    meta: { title: '账户' },
  },
  {
    path: '/advisor',
    name: 'advisor',
    component: () => import('@/features/chat/views/AdvisorView.vue'),
    meta: { title: '投顾' },
  },
  {
    // AI 账户与用户持仓的收益对比：独立成页，因为它是长期的、可反复查看的视图，
    // 不适合挤在对话气泡（宽度只有 720px）里。
    path: '/portfolio/compare',
    name: 'portfolio-compare',
    component: () => import('@/features/portfolio/views/PortfolioCompareView.vue'),
    meta: { title: '收益对比' },
  },
  {
    // 管理后台按功能分区成多个页面，避免所有面板堆在同一页。
    // 每个子路由都显式标注 adminOnly，不依赖父记录 meta 的隐式合并 ——
    // 守卫漏判会让普通用户直接看到后台，这个代价太高，不值得省这几行。
    path: '/admin',
    meta: { adminOnly: true },
    children: [
      { path: '', redirect: { name: 'admin-users' } },
      {
        path: 'users',
        name: 'admin-users',
        component: () => import('@/features/admin/components/AdminUserPanel.vue'),
        meta: { title: '用户与持仓', adminOnly: true },
      },
      {
        path: 'products',
        name: 'admin-products',
        component: () => import('@/features/admin/components/AdminProductPanel.vue'),
        meta: { title: '商品管理', adminOnly: true },
      },
    ],
  },
  { path: '/:pathMatch(.*)*', redirect: '/market' },
]

export const router = createRouter({
  history: createWebHistory(),
  routes,
})

// 登录门禁 + 角色分流：未登录显示登录页；管理员只去后台，普通用户只去用户侧。
router.beforeEach((to) => {
  const user = getStoredUser()

  // 未登录不重定向：登录页由 App.vue 依据登录态渲染，与本路由无关，因此不会
  // 泄漏任何受保护内容（此时 router-view 根本不挂载）。
  // 守卫这里**不能**改写为 redirect 到某个受保护地址 —— 再次命中同一地址时，
  // Vue Router 会判定为无限重定向并中止整次导航，反而把地址栏留在原处。
  if (!user?.token) return true

  // 角色缺失说明本地登录态早于角色字段（或尚未刷新）：一律按普通用户放行，
  // 绝不因此渲染管理后台。App.vue 刷新身份后会把真正的管理员送回后台。
  const admin = user.is_admin === true

  if (admin && !to.meta.adminOnly) return { name: 'admin-users' }
  if (!admin && to.meta.adminOnly) return { name: 'market' }
  return true
})

export default router
