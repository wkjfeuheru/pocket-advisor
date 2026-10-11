<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { SwitchButton } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import LoginView from '@/features/auth/components/LoginView.vue'
import { getCurrentUser, logout } from '@/features/auth/api'
import { getStoredUser, saveUser, clearUser } from '@/app/session'
import type { UserInfo } from '@/features/auth/types'

const route = useRoute()
const router = useRouter()

const currentUser = ref<UserInfo | null>(null)

const displayName = computed(() => currentUser.value?.display_name || currentUser.value?.username || '')
const isLoggedIn = computed(() => !!currentUser.value)
const isAdmin = computed(() => currentUser.value?.is_admin === true)

/** 顶部导航项；交易入口均在登录后可见。 */
const navItems = [
  { name: 'market', label: '商品' },
  { name: 'positions', label: '持仓' },
  { name: 'account', label: '账户' },
  { name: 'advisor', label: '投顾' },
]

/** 后台分区导航：每个面板一个页面，不堆在同一页。 */
const adminNavItems = [
  { name: 'admin-users', label: '用户与持仓' },
  { name: 'admin-products', label: '商品管理' },
]

/**
 * 从服务端刷新身份，使 localStorage 中的陈旧态（如缺少 is_admin）与后端一致。
 * 失败时保留本地登录态，仅视为非管理员。
 */
async function refreshIdentity() {
  if (!currentUser.value?.token) return
  try {
    const me = await getCurrentUser()
    currentUser.value = { ...currentUser.value, ...me }
    saveUser(currentUser.value)
    await syncRouteWithRole()
  } catch (error: any) {
    if (error?.response?.status === 401) {
      clearUser()
      currentUser.value = null
    }
  }
}

function handleAuthExpired() {
  currentUser.value = null
}

/**
 * 角色决定界面：管理员只停留在 /admin，普通用户不允许进入 /admin。
 *
 * 服务端返回的角色可能与本地缓存的登录态不一致（如角色刚被授予或撤销），
 * 因此身份刷新后必须重新校正路由，否则会渲染出对方的界面。
 */
async function syncRouteWithRole() {
  if (isAdmin.value && !route.meta.adminOnly) {
    await router.replace({ name: 'admin-users' })
  } else if (!isAdmin.value && route.meta.adminOnly) {
    await router.replace({ name: 'market' })
  }
}

/** 登录成功回调 */
async function handleLoggedIn(user: UserInfo) {
  currentUser.value = user
  await refreshIdentity()
}

/** 登出 */
async function handleLogout() {
  try {
    await ElMessageBox.confirm(
      `确定要退出当前账号（${displayName.value}）吗？`,
      '登出确认',
      {
        confirmButtonText: '确定登出',
        cancelButtonText: '取消',
        type: 'warning',
      },
    )
  } catch {
    return
  }
  await logout()
  currentUser.value = null
  await router.replace({ name: 'market' })
  ElMessage.success('已登出')
}

async function goTo(name: string) {
  if (route.name !== name) await router.push({ name })
}

onMounted(async () => {
  window.addEventListener('auth:expired', handleAuthExpired)
  const stored = getStoredUser()
  if (stored && stored.token) {
    currentUser.value = stored
    await refreshIdentity()
  }
})

onUnmounted(() => {
  window.removeEventListener('auth:expired', handleAuthExpired)
})
</script>

<template>
  <!-- 未登录：显示登录页 -->
  <LoginView v-if="!isLoggedIn" @logged-in="handleLoggedIn" />

  <!-- 管理员：独立后台界面，不渲染任何用户侧页面（对话/商品/持仓/账户） -->
  <div v-else-if="isAdmin" class="app-layout">
    <header class="app-header">
      <div class="header-inner">
        <div class="brand">
          <span class="brand-mark">¥</span>
          <div class="brand-copy">
            <h1 class="app-title">金融智能投顾系统</h1>
            <span class="brand-meta">ADMIN CONSOLE / 管理后台</span>
          </div>
        </div>

        <nav class="app-nav" aria-label="后台导航">
          <button
            v-for="item in adminNavItems"
            :key="item.name"
            type="button"
            class="nav-link"
            :class="{ active: route.name === item.name }"
            :aria-current="route.name === item.name ? 'page' : undefined"
            @click="goTo(item.name)"
          >
            {{ item.label }}
          </button>
        </nav>

        <div class="header-right">
          <span class="system-status"><i></i>系统在线</span>
          <el-tag type="success" effect="plain">管理员</el-tag>
          <el-tag type="info" effect="plain">{{ displayName }}</el-tag>
          <el-tooltip content="登出" placement="bottom">
            <el-button
              class="logout-btn"
              circle
              size="small"
              @click="handleLogout"
            >
              <el-icon size="16"><SwitchButton /></el-icon>
            </el-button>
          </el-tooltip>
        </div>
      </div>
    </header>

    <!-- 后台每个分区一个页面，各自独立加载与鉴权 -->
    <main class="admin-page">
      <router-view />
    </main>
  </div>

  <!-- 普通用户：顶部导航 + 路由页面 -->
  <div v-else class="app-layout">
    <header class="app-header">
      <div class="header-inner">
        <div class="brand">
          <span class="brand-mark">¥</span>
          <div class="brand-copy">
            <h1 class="app-title">金融智能投顾系统</h1>
            <span class="brand-meta">FINANCE INTELLIGENCE / WORKSPACE</span>
          </div>
        </div>

        <nav class="app-nav" aria-label="主导航">
          <button
            v-for="item in navItems"
            :key="item.name"
            type="button"
            class="nav-link"
            :class="{ active: route.name === item.name }"
            :aria-current="route.name === item.name ? 'page' : undefined"
            @click="goTo(item.name)"
          >
            {{ item.label }}
          </button>
        </nav>

        <div class="header-right">
          <span class="system-status"><i></i>系统在线</span>
          <el-tag type="info" effect="plain">
            {{ displayName }}
          </el-tag>
          <el-tooltip content="登出" placement="bottom">
            <el-button
              class="logout-btn"
              circle
              size="small"
              @click="handleLogout"
            >
              <el-icon size="16"><SwitchButton /></el-icon>
            </el-button>
          </el-tooltip>
        </div>
      </div>
    </header>

    <!-- 必须有一层 flex 占位：否则 RouterView 不参与 flex，
         商品/持仓页的 min-height:0 会把表格高度算成 0，看起来像没有数据。 -->
    <div class="app-page">
      <router-view v-slot="{ Component }">
        <component :is="Component" :current-user="currentUser" />
      </router-view>
    </div>
  </div>
</template>

<style scoped>
.app-layout {
  display: flex;
  flex-direction: column;
  flex: 1;
  min-height: 0;
  overflow: hidden;
  background: transparent;
}

.app-page {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

/* 后台页面容器：单栏滚动，内容居中限宽，避免面板在大屏上被拉得过宽。 */
.admin-page {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 28px 28px 48px;
}
.admin-page :deep(> *) {
  max-width: 1040px;
  margin: 0 auto;
}

/* 顶部栏：半透明象牙 + 模糊 + 细线分隔，滚动时保持可读。 */
.app-header {
  background: rgba(250, 248, 242, 0.85);
  backdrop-filter: blur(10px);
  border-bottom: 1px solid var(--color-border);
  position: relative;
  z-index: 100;
  flex-shrink: 0;
}

.header-inner {
  display: flex;
  align-items: center;
  gap: 18px;
  padding: 0 26px;
  height: 66px;
  max-width: 100%;
}

.brand {
  display: flex;
  align-items: center;
  gap: 12px;
  min-width: 0;
}

.brand-mark {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 38px;
  height: 38px;
  border-radius: 10px;
  background: linear-gradient(150deg, var(--color-primary-light), var(--color-primary));
  color: #fff;
  font-weight: 800;
  font-size: 19px;
  box-shadow: var(--shadow-sm), inset 0 0 0 1px rgba(255, 255, 255, 0.1);
}

.brand-copy { display: flex; flex-direction: column; line-height: 1.15; min-width: 0; }
/* 字号放在低特异性规则里，好让下面的窄屏媒体查询可以覆盖它。 */
.app-title {
  margin: 0;
  font-family: var(--font-display);
  font-size: 18px;
  font-weight: 600;
  letter-spacing: 0.02em;
}
.brand-copy .app-title {
  color: var(--color-primary);
}
.brand-meta {
  margin-top: 4px;
  color: var(--color-text-muted);
  font: 10px/1 var(--font-mono);
  letter-spacing: 0.18em;
  white-space: nowrap;
}

/* 主导航：文字按钮 + 金铜下划线激活态，与编辑部气质一致 */
.app-nav {
  display: flex;
  align-items: center;
  gap: 2px;
  margin-left: 14px;
}

.nav-link {
  position: relative;
  border: 0;
  background: transparent;
  padding: 9px 14px;
  color: var(--color-text-secondary);
  font-size: 13px;
  cursor: pointer;
  border-radius: var(--radius);
  transition: color 0.16s ease, background 0.16s ease;
}
.nav-link:hover { color: var(--color-primary); background: var(--color-primary-soft); }
.nav-link.active { color: var(--color-primary); font-weight: 600; }
.nav-link.active::after {
  content: '';
  position: absolute;
  left: 14px;
  right: 14px;
  bottom: 3px;
  height: 2px;
  border-radius: 2px;
  background: var(--color-accent);
}

.header-right {
  margin-left: auto;
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
}
.header-right :deep(.el-tag) {
  max-width: 150px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  border-radius: var(--radius-sm);
}

.logout-btn {
  background: transparent;
  border: 1px solid var(--color-border);
  color: var(--color-text-secondary);
}
.logout-btn:hover {
  background: rgba(179, 68, 47, 0.08);
  color: var(--color-danger);
  border-color: var(--color-danger);
}

.system-status {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  border: 1px solid var(--color-border);
  border-radius: 999px;
  padding: 5px 11px;
  color: var(--color-text-secondary);
  font: 10px/1 var(--font-mono);
  letter-spacing: 0.1em;
}
.system-status i {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--color-success);
  box-shadow: 0 0 0 3px rgba(44, 122, 83, 0.16);
}

/* 响应式：窄屏隐藏导航文字标签之外的元素，导航允许横向滚动 */
@media (max-width: 768px) {
  .header-inner { padding: 0 12px; gap: 10px; }
  .brand-mark { flex-shrink: 0; }
  .brand-meta, .system-status { display: none; }
  .app-title { font-size: 15px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .app-nav {
    margin-left: 0;
    overflow-x: auto;
    scrollbar-width: none;
  }
  .app-nav::-webkit-scrollbar { display: none; }
  .nav-link { padding: 8px 8px; white-space: nowrap; }
  .header-right { gap: 6px; flex-shrink: 0; }
}

@media (max-width: 480px) {
  .header-right :deep(.el-tag) { display: none; }
  .brand-mark { width: 30px; height: 30px; }
  .brand-copy { display: none; }
}
</style>
