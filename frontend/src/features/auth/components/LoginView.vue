<script setup lang="ts">
import { ref, reactive, computed } from 'vue'
import { ElMessage } from 'element-plus'
import { User, Lock, EditPen } from '@element-plus/icons-vue'
import { login, register } from '@/features/auth/api'
import { apiErrorMessage } from '@/api/errors'
import { saveUser } from '@/app/session'
import type { UserInfo } from '@/features/auth/types'

const emit = defineEmits<{
  (event: 'logged-in', user: UserInfo): void
}>()

type TabKey = 'login' | 'register'
const activeTab = ref<TabKey>('login')
const submitting = ref(false)

const loginForm = reactive({
  username: '',
  password: '',
})
const registerForm = reactive({
  username: '',
  password: '',
  confirmPassword: '',
  displayName: '',
})

const loginFormRef = ref()
const registerFormRef = ref()

const loginRules = {
  username: [{ required: true, message: '请输入用户名', trigger: 'blur' }],
  password: [{ required: true, message: '请输入密码', trigger: 'blur' }],
}

const registerRules = {
  username: [
    { required: true, message: '请输入用户名', trigger: 'blur' },
    { min: 2, max: 32, message: '长度需为 2-32 个字符', trigger: 'blur' },
  ],
  password: [
    { required: true, message: '请输入密码', trigger: 'blur' },
    { min: 6, max: 64, message: '长度需为 6-64 个字符', trigger: 'blur' },
  ],
  confirmPassword: [
    { required: true, message: '请再次输入密码', trigger: 'blur' },
    {
      validator: (_rule: any, value: string, callback: any) => {
        if (value !== registerForm.password) {
          callback(new Error('两次输入的密码不一致'))
        } else {
          callback()
        }
      },
      trigger: 'blur',
    },
  ],
}

const currentTitle = computed(() => (activeTab.value === 'login' ? '欢迎回来' : '创建账号'))
const currentSubtitle = computed(() =>
  activeTab.value === 'login' ? '登录以开始您的智能投顾会话' : '注册一个新账号以使用投顾系统',
)

async function handleLogin() {
  if (!loginFormRef.value) return
  try {
    await loginFormRef.value.validate()
  } catch {
    return
  }
  submitting.value = true
  try {
    const res = await login({
      username: loginForm.username.trim(),
      password: loginForm.password,
    })
    const user: UserInfo = {
      customer_id: res.customer_id,
      username: res.username,
      display_name: res.display_name || res.username,
      token: res.token,
      expires_in: res.expires_in,
      login_at: Date.now(),
      is_admin: res.is_admin === true,
    }
    saveUser(user)
    ElMessage.success(`欢迎回来，${user.display_name}！`)
    emit('logged-in', user)
  } catch (err: any) {
    ElMessage.error(apiErrorMessage(err, '登录失败'))
  } finally {
    submitting.value = false
  }
}

async function handleRegister() {
  if (!registerFormRef.value) return
  try {
    await registerFormRef.value.validate()
  } catch {
    return
  }
  submitting.value = true
  try {
    await register({
      username: registerForm.username.trim(),
      password: registerForm.password,
      display_name: registerForm.displayName.trim(),
    })
    ElMessage.success('注册成功，请使用新账号登录')
    // 注册成功后切到登录页，预填用户名
    loginForm.username = registerForm.username.trim()
    loginForm.password = ''
    activeTab.value = 'login'
    registerForm.password = ''
    registerForm.confirmPassword = ''
  } catch (err: any) {
    ElMessage.error(apiErrorMessage(err, '注册失败'))
  } finally {
    submitting.value = false
  }
}

function switchTab(tab: TabKey) {
  if (submitting.value) return
  activeTab.value = tab
}
</script>

<template>
  <div class="login-view">
    <div class="login-bg"></div>
    <section class="trust-panel rise-in">
      <div class="trust-brand"><span>¥</span> FINANCE AGENT / 01</div>
      <h1>让每一次投资决策<br />都有清晰依据。</h1>
      <p>多 Agent 协作完成行情研究、标的筛选、风险画像与资产配置。</p>
      <div class="capability-grid">
        <div><b>01</b><span>多意图识别</span></div>
        <div><b>02</b><span>实时分析链路</span></div>
        <div><b>03</b><span>合规风险控制</span></div>
        <div><b>04</b><span>个性化配置</span></div>
      </div>
      <div class="trust-status"><i></i> 服务状态正常 · 数据安全连接</div>
    </section>
    <div class="login-card rise-in">
      <div class="brand-row">
        <div class="brand-mark">¥</div>
        <div class="brand-text">
          <h1 class="brand-title">金融智能投顾系统</h1>
          <p class="brand-sub">多 Agent 协作 · 合规风控 · 资产配置</p>
        </div>
      </div>

      <div class="tabs">
        <button
          class="tab"
          :class="{ active: activeTab === 'login' }"
          @click="switchTab('login')"
        >
          登录
        </button>
        <button
          class="tab"
          :class="{ active: activeTab === 'register' }"
          @click="switchTab('register')"
        >
          注册
        </button>
      </div>

      <div class="form-header">
        <h2 class="form-title">{{ currentTitle }}</h2>
        <p class="form-subtitle">{{ currentSubtitle }}</p>
      </div>

      <!-- 登录表单 -->
      <el-form
        v-if="activeTab === 'login'"
        ref="loginFormRef"
        :model="loginForm"
        :rules="loginRules"
        label-position="top"
        @submit.prevent="handleLogin"
      >
        <el-form-item label="用户名" prop="username">
          <el-input
            v-model="loginForm.username"
            placeholder="请输入用户名"
            :prefix-icon="User"
            clearable
            @keyup.enter="handleLogin"
          />
        </el-form-item>
        <el-form-item label="密码" prop="password">
          <el-input
            v-model="loginForm.password"
            type="password"
            placeholder="请输入密码"
            :prefix-icon="Lock"
            show-password
            @keyup.enter="handleLogin"
          />
        </el-form-item>
        <el-button
          type="primary"
          class="submit-btn"
          :loading="submitting"
          @click="handleLogin"
        >
          登录
        </el-button>
      </el-form>

      <!-- 注册表单 -->
      <el-form
        v-else
        ref="registerFormRef"
        :model="registerForm"
        :rules="registerRules"
        label-position="top"
        @submit.prevent="handleRegister"
      >
        <el-form-item label="用户名" prop="username">
          <el-input
            v-model="registerForm.username"
            placeholder="2-32 个字符"
            :prefix-icon="User"
            clearable
          />
        </el-form-item>
        <el-form-item label="显示名称（可选）" prop="displayName">
          <el-input
            v-model="registerForm.displayName"
            placeholder="留空则默认使用用户名"
            :prefix-icon="EditPen"
            clearable
          />
        </el-form-item>
        <el-form-item label="密码" prop="password">
          <el-input
            v-model="registerForm.password"
            type="password"
            placeholder="6-64 个字符"
            :prefix-icon="Lock"
            show-password
          />
        </el-form-item>
        <el-form-item label="确认密码" prop="confirmPassword">
          <el-input
            v-model="registerForm.confirmPassword"
            type="password"
            placeholder="请再次输入密码"
            :prefix-icon="Lock"
            show-password
            @keyup.enter="handleRegister"
          />
        </el-form-item>
        <el-button
          type="primary"
          class="submit-btn"
          :loading="submitting"
          @click="handleRegister"
        >
          注册
        </el-button>
      </el-form>

      <p class="hint">
        {{ activeTab === 'login' ? '还没有账号？' : '已有账号？' }}
        <a
          href="javascript:void(0)"
          @click="switchTab(activeTab === 'login' ? 'register' : 'login')"
        >
          {{ activeTab === 'login' ? '立即注册' : '去登录' }}
        </a>
      </p>
    </div>
  </div>
</template>

<style scoped>
.login-view {
  position: relative;
  min-height: 100%;
  width: 100%;
  display: flex;
  align-items: stretch;
  justify-content: center;
  padding: 0;
  overflow: hidden;
}

/* 细网格纸底 + 双角渐晕，营造"研究纸张"氛围 */
.login-bg {
  position: absolute;
  inset: 0;
  background-color: var(--color-bg);
  background-image:
    linear-gradient(var(--grid-line) 1px, transparent 1px),
    linear-gradient(90deg, var(--grid-line) 1px, transparent 1px);
  background-size: 88px 88px;
}
.login-bg::before {
  content: '';
  position: absolute;
  inset: 0;
  background:
    radial-gradient(circle at 12% 8%, rgba(185, 137, 63, 0.12), transparent 46%),
    radial-gradient(circle at 88% 92%, rgba(23, 54, 42, 0.1), transparent 50%);
}

.trust-panel {
  position: relative;
  z-index: 1;
  width: min(54%, 760px);
  padding: clamp(48px, 8vw, 120px);
  display: flex;
  flex-direction: column;
  justify-content: center;
  border-right: 1px solid var(--color-border);
}
.trust-brand {
  display: flex;
  align-items: center;
  color: var(--color-primary);
  font: 11px/1 var(--font-mono);
  letter-spacing: 0.18em;
}
.trust-brand span {
  display: inline-flex;
  width: 30px;
  height: 30px;
  align-items: center;
  justify-content: center;
  margin-right: 12px;
  border-radius: 8px;
  background: linear-gradient(150deg, var(--color-primary-light), var(--color-primary));
  color: #fff;
  font: 700 15px/1 var(--font-display);
}
.trust-panel h1 {
  margin: 56px 0 24px;
  font-family: var(--font-display);
  color: var(--color-primary);
  font-size: clamp(38px, 4.6vw, 68px);
  line-height: 1.08;
  letter-spacing: -0.01em;
  font-weight: 600;
}
.trust-panel > p {
  max-width: 540px;
  margin: 0;
  color: var(--color-text-secondary);
  font-size: 16px;
  line-height: 1.9;
}
.capability-grid {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  margin-top: 56px;
  border-top: 1px solid var(--color-border);
  border-left: 1px solid var(--color-border);
}
.capability-grid div {
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding: 22px;
  border-right: 1px solid var(--color-border);
  border-bottom: 1px solid var(--color-border);
  background: rgba(255, 254, 251, 0.55);
  transition: background 0.2s ease, transform 0.2s ease;
}
.capability-grid div:hover {
  background: var(--color-surface);
  transform: translateY(-2px);
}
.capability-grid b {
  color: var(--color-accent);
  font: 11px/1 var(--font-mono);
  letter-spacing: 0.1em;
}
.capability-grid span {
  color: var(--color-text);
  font-weight: 600;
}
.trust-status {
  margin-top: auto;
  padding-top: 40px;
  color: var(--color-text-muted);
  font: 10px/1 var(--font-mono);
  letter-spacing: 0.12em;
}
.trust-status i {
  display: inline-block;
  width: 7px;
  height: 7px;
  border-radius: 50%;
  margin-right: 8px;
  background: var(--color-success);
  box-shadow: 0 0 0 3px rgba(44, 122, 83, 0.16);
}

.login-card {
  position: relative;
  z-index: 1;
  align-self: center;
  width: 100%;
  max-width: 440px;
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-lg);
  padding: 42px 40px;
  margin: 40px clamp(28px, 6vw, 96px);
}

.brand-row {
  display: flex;
  align-items: center;
  gap: 14px;
  margin-bottom: 26px;
}

.brand-mark {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 46px;
  height: 46px;
  border-radius: 12px;
  background: linear-gradient(150deg, var(--color-primary-light), var(--color-primary));
  color: #fff;
  font: 800 22px/1 var(--font-display);
  box-shadow: var(--shadow-sm);
}

.brand-text {
  display: flex;
  flex-direction: column;
}

.brand-title {
  margin: 0;
  font-family: var(--font-display);
  font-size: 19px;
  font-weight: 600;
  color: var(--color-primary);
  letter-spacing: 0.02em;
}

.brand-sub {
  margin: 3px 0 0;
  font-size: 12px;
  color: var(--color-text-muted);
}

.tabs {
  display: flex;
  gap: 3px;
  background: var(--color-surface-alt);
  border: 1px solid var(--color-border);
  border-radius: var(--radius);
  padding: 3px;
  margin-bottom: 22px;
}

.tab {
  flex: 1;
  border: none;
  background: transparent;
  padding: 9px 12px;
  border-radius: var(--radius-sm);
  cursor: pointer;
  font-size: 14px;
  font-weight: 600;
  color: var(--color-text-secondary);
  transition: all 0.18s ease;
}

.tab:hover {
  color: var(--color-text);
}

.tab.active {
  background: var(--color-surface);
  color: var(--color-primary);
  box-shadow: var(--shadow-sm);
}

.form-header {
  margin-bottom: 18px;
}

.form-title {
  margin: 0;
  font-family: var(--font-display);
  font-size: 22px;
  font-weight: 600;
  color: var(--color-primary);
}

.form-subtitle {
  margin: 5px 0 0;
  font-size: 13px;
  color: var(--color-text-muted);
}

.submit-btn {
  width: 100%;
  height: 44px;
  font-size: 15px;
  font-weight: 600;
  margin-top: 6px;
  border-radius: var(--radius);
}

.hint {
  margin: 20px 0 0;
  text-align: center;
  font-size: 13px;
  color: var(--color-text-secondary);
}

.hint a {
  color: var(--color-primary);
  text-decoration: none;
  font-weight: 600;
}
.hint a:hover {
  text-decoration: underline;
}

@media (max-width: 900px) {
  .login-view { padding: 24px; }
  .trust-panel { display: none; }
  .login-card { margin: 0; }
}

@media (max-width: 480px) {
  .login-view { padding: 20px; }
  .login-card { padding: 30px 22px 22px; }
  .brand-title { font-size: 17px; }
}
</style>
