<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { renderMarkdown, renderMarkdownStreaming } from '@/shared/markdown'
import {
  consumeSse,
  deleteSession,
  getProfile,
  getRun,
  listMessages,
  listSessions,
  saveProfile,
  type ChatMessage,
  type Profile,
  type SessionItem,
} from '../api'
import { allocationAdoptPath, clarifyStreamPath, optionsForField } from '../artifacts'
import AllocationPlanCards from '../components/AllocationPlanCards.vue'
import StockArtifacts from '../components/StockArtifacts.vue'
import {
  PROFILE_FIELDS,
  applyRunSnapshot,
  beginTurn,
  initialTurn,
  mergeMessageHistory,
  missingProfileFields,
  profileOptionLabel,
  reduceTurn,
  turnErrorMessage,
  type TurnState,
} from '../state'

const sessions = ref<SessionItem[]>([])
const selectedId = ref<string | null>(null)
const history = ref<ChatMessage[]>([])
const draft = ref('')
const turn = ref<TurnState>(initialTurn())
const profile = ref<Profile | null>(null)
const profileOpen = ref(false)
const profileDraft = ref<Record<string, string | number>>({})
const resumeDraft = ref<Record<string, string | number>>({})
const clarifyDraft = ref<Record<string, string>>({})

/** 消息滚动容器与当前流式请求的中止控制器。 */
const messagesEl = ref<HTMLElement | null>(null)
let controller: AbortController | null = null

/**
 * 读取草稿里的字段并归一成去空白的字符串。
 *
 * `<input type="number">` 配合 v-model 会得到 number（或空串），直接调 `.trim()`
 * 会抛 `trim is not a function` 并中断提交；这里统一收敛。
 */
function draftText(draft: Record<string, string | number>, id: string): string {
  const value = draft[id]
  return value == null ? '' : String(value).trim()
}

const running = computed(() => turn.value.status === 'running')
const rendered = computed(() => renderMarkdownStreaming(turn.value.answer, turn.value.status !== 'running'))
const promptedFields = computed(() => missingProfileFields(turn.value.missingFields))

/** 历史消息里的助手回复也走 Markdown 渲染，避免刷新后出现字面语法。 */
function historyBody(content: string): string {
  return renderMarkdown(content)
}

/** 只有当用户本就在底部附近时才自动跟随，避免打断向上翻阅。 */
async function scrollToLatest(force = false) {
  await nextTick()
  const el = messagesEl.value
  if (!el) return
  const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 160
  if (force || nearBottom) el.scrollTop = el.scrollHeight
}

async function refreshSessions() {
  sessions.value = await listSessions()
}

function startNewChat() {
  selectedId.value = null
  history.value = []
  turn.value = initialTurn()
  draft.value = ''
}

async function openSession(id: string) {
  selectedId.value = id
  history.value = await listMessages(id)
  turn.value = initialTurn()
  turn.value.sessionId = id
  await scrollToLatest(true)
}

async function removeSession(id: string) {
  await deleteSession(id)
  if (selectedId.value === id) startNewChat()
  await refreshSessions()
}

function onEvent(event: { event: string; data: unknown }) {
  turn.value = reduceTurn(turn.value, event)
  if (event.event === 'run') {
    const data = event.data as { session_id?: string }
    if (data.session_id) selectedId.value = data.session_id
  }
  if (event.event === 'profile_required') {
    resumeDraft.value = {}
    for (const field of promptedFields.value) {
      const current = turn.value.profile[field.id as keyof typeof turn.value.profile]
      resumeDraft.value[field.id] = current == null ? '' : String(current)
    }
  }
  if (event.event === 'clarification_required') {
    clarifyDraft.value = {}
  }
}

async function recover(requestId: string) {
  const run = await getRun(requestId)
  turn.value = applyRunSnapshot(turn.value, run)
  if (run.session_id) selectedId.value = run.session_id
  if (turn.value.clarification) clarifyDraft.value = {}
}

async function refreshCompletedHistory() {
  const sessionId = turn.value.sessionId || selectedId.value
  if (turn.value.status !== 'completed' || !sessionId || selectedId.value !== sessionId) return
  const serverHistory = await listMessages(sessionId)
  history.value = mergeMessageHistory(history.value, serverHistory)
  const archived = history.value.some(
    (message) => message.request_id === turn.value.requestId && message.role === 'assistant',
  )
  if (archived) turn.value = { ...turn.value, answer: '', artifacts: [] }
}

/** 用户主动停止本轮：中止 SSE，保留已收到的部分文本并解锁输入。 */
function stop() {
  controller?.abort()
}

async function send(path: string, body: unknown, requestId: string | null = turn.value.requestId) {
  if (running.value) return
  turn.value = beginTurn(turn.value, requestId)
  const abort = new AbortController()
  controller = abort
  try {
    await consumeSse(path, body, onEvent, abort.signal)
  } catch (error: any) {
    if (abort.signal.aborted) {
      // 主动停止：后端会把断流记为客户断开，这里只把本地状态收回到可重发的完成态。
      turn.value = { ...turn.value, status: 'completed', stage: '已停止' }
      return
    }
    if (error?.code === 'graph_version_changed') {
      turn.value = reduceTurn(turn.value, { event: 'error', data: { code: error.code, retryable: false } })
      return
    }
    if (turn.value.requestId) {
      try {
        await recover(turn.value.requestId)
        return
      } catch {
        // 查询运行状态失败时保留本地错误。
      }
    }
    turn.value = { ...turn.value, status: 'failed', retryable: true, errorCode: 'stream_lost' }
    ElMessage.error(error?.message || '连接中断')
  } finally {
    controller = null
    await refreshSessions().catch(() => undefined)
    await refreshCompletedHistory().catch(() => undefined)
  }
}

async function submit() {
  const message = draft.value.trim()
  if (!message || running.value) return
  const requestId = crypto.randomUUID()
  draft.value = ''
  history.value = [
    ...history.value,
    { id: requestId, request_id: requestId, role: 'user', content: message, created_at: '' },
  ]
  await scrollToLatest(true)
  await send('/agents/chat/stream', {
    request_id: requestId,
    session_id: selectedId.value,
    message,
  }, requestId)
}

async function submitResume() {
  if (!turn.value.requestId) return
  const patch: Record<string, string | number> = {}
  for (const field of promptedFields.value) {
    const value = draftText(resumeDraft.value, field.id)
    if (!value) {
      ElMessage.warning(`请填写${field.label}`)
      return
    }
    patch[field.id] = field.id === 'budget_amount' ? Number(value) : value
  }
  await send(`/agents/runs/${turn.value.requestId}/resume/stream`, { profile: patch })
  profile.value = await getProfile()
}

async function submitAllocation(planId: string) {
  if (!turn.value.requestId || !planId) return
  await send(allocationAdoptPath(turn.value.requestId), { plan_id: planId })
}

async function submitClarify() {
  if (!turn.value.requestId || !turn.value.clarification) return
  const answers: Record<string, string> = {}
  for (const field of turn.value.clarification.fields) {
    const value = (clarifyDraft.value[field] || '').trim()
    if (!value) {
      ElMessage.warning('请先选择澄清项')
      return
    }
    answers[field] = value
  }
  await send(clarifyStreamPath(turn.value.requestId), { answers })
}

async function retry() {
  if (!turn.value.requestId || !turn.value.retryable) return
  await send(`/agents/runs/${turn.value.requestId}/retry/stream`, {})
}

function openProfile() {
  profileDraft.value = {
    risk_level: profile.value?.risk_level || '',
    investment_horizon: profile.value?.investment_horizon || '',
    investment_goal: profile.value?.investment_goal || '',
    liquidity_need: profile.value?.liquidity_need || '',
    budget_amount: profile.value?.budget_amount == null ? '' : String(profile.value.budget_amount),
  }
  profileOpen.value = true
}

async function submitProfile() {
  const patch: Record<string, string | number> = {}
  for (const field of PROFILE_FIELDS) {
    const value = draftText(profileDraft.value, field.id)
    if (!value) continue
    patch[field.id] = field.id === 'budget_amount' ? Number(value) : value
  }
  if (!Object.keys(patch).length) {
    ElMessage.warning('请至少填写一项后再保存')
    return
  }
  profile.value = await saveProfile(patch)
  profileOpen.value = false
  ElMessage.success('画像已保存')
}

function currentProfileValue(id: string): string {
  const value = turn.value.profile[id as keyof typeof turn.value.profile]
  return value == null || value === '' ? '' : profileOptionLabel(id, String(value))
}

// 答案/状态/历史变化时自动滚动到最新（靠近底部时）。
watch(
  () => [turn.value.answer, turn.value.status, turn.value.warnings.length, history.value.length],
  () => scrollToLatest(),
)

onMounted(async () => {
  await refreshSessions()
  profile.value = await getProfile()
})

onUnmounted(() => {
  controller?.abort()
})
</script>

<template>
  <div class="advisor">
    <aside class="sessions">
      <button type="button" class="new-chat" @click="startNewChat">新对话</button>
      <button
        v-for="item in sessions"
        :key="item.id"
        type="button"
        class="session"
        :class="{ active: item.id === selectedId }"
        @click="openSession(item.id)"
      >
        <span>{{ item.title || '未命名' }}</span>
        <em @click.stop="removeSession(item.id)">删除</em>
      </button>
    </aside>

    <section class="thread">
      <header class="thread-bar">
        <span class="stage" :class="{ running }">{{ turn.stage || (running ? '处理中' : '待命') }}</span>
        <button type="button" class="profile-entry" @click="openProfile">风险画像</button>
      </header>

      <div ref="messagesEl" class="messages">
        <article v-for="item in history" :key="item.id" class="bubble" :class="item.role">
          <div v-if="item.role === 'assistant'" class="bubble-body">
            <div v-if="item.content" class="markdown-body" v-html="historyBody(item.content)"></div>
            <div
              v-if="item.public_metadata && (item.public_metadata.artifacts || []).length"
              class="evidence-block"
            >
              <h4>证据</h4>
              <StockArtifacts :artifacts="item.public_metadata?.artifacts" />
            </div>
          </div>
          <p v-else-if="item.content" class="user-text">{{ item.content }}</p>
        </article>

        <article v-if="turn.answer || running || turn.artifacts.length" class="bubble assistant">
          <div class="bubble-body">
            <div v-if="rendered.settled" class="markdown-body" v-html="rendered.settled"></div>
            <span v-if="rendered.pending" class="markdown-pending">{{ rendered.pending }}</span>

            <!-- 尚无正文时的生成中占位，替代原本的空白气泡 -->
            <div v-if="running && !turn.answer" class="generating">
              <span class="dot"></span><span class="dot"></span><span class="dot"></span>
              <em>{{ turn.stage || '生成中' }}</em>
            </div>

            <div v-if="turn.artifacts.length" class="evidence-block">
              <h4>证据</h4>
              <StockArtifacts :artifacts="turn.artifacts" />
            </div>
          </div>
        </article>

        <section v-if="turn.warnings.length" class="notice-block">
          <h4>提示</h4>
          <ul>
            <li v-for="warning in turn.warnings" :key="warning">{{ warning }}</li>
          </ul>
        </section>

        <p v-if="turn.status === 'failed'" class="warning">
          {{ turnErrorMessage(turn) }}
          <button v-if="turn.retryable" type="button" @click="retry">重试</button>
        </p>
      </div>

      <form v-if="turn.status === 'interrupted' && turn.allocationPlans.length" class="resume" @submit.prevent>
        <p>请选择一个配置方案。采纳后只写入 AI 账户，不会改动你的真实持仓。</p>
        <AllocationPlanCards
          :plans="turn.allocationPlans"
          :disabled="running"
          @select="submitAllocation"
        />
      </form>
      <form v-else-if="turn.status === 'interrupted' && turn.clarification" class="resume" @submit.prevent="submitClarify">
        <p>{{ turn.clarification.question || '请选择要继续分析的标的或策略。' }}</p>
        <label v-for="field in turn.clarification.fields" :key="field">
          {{ field }}
          <select v-model="clarifyDraft[field]">
            <option value="">请选择</option>
            <option
              v-for="option in optionsForField(turn.clarification.options, field)"
              :key="option.id"
              :value="option.id"
            >{{ option.label }}</option>
          </select>
        </label>
        <button type="submit" :disabled="running">确认澄清</button>
      </form>
      <form v-else-if="turn.status === 'interrupted'" class="resume" @submit.prevent="submitResume">
        <p>继续回答前，请确认本轮缺少的画像。</p>
        <label v-for="field in promptedFields" :key="field.id">
          {{ field.label }}
          <select v-if="field.options.length" v-model="resumeDraft[field.id]">
            <option value="">请选择</option>
            <option v-for="option in field.options" :key="option" :value="option">{{ profileOptionLabel(field.id, option) }}</option>
          </select>
          <input v-else v-model="resumeDraft[field.id]" type="number" min="0" step="0.01" />
          <small v-if="currentProfileValue(field.id)">当前：{{ currentProfileValue(field.id) }}</small>
        </label>
        <button type="submit" :disabled="running">确认并继续</button>
      </form>

      <form class="composer" @submit.prevent="submit">
        <textarea v-model="draft" :disabled="running" rows="3" placeholder="输入投资问题" @keydown.enter.exact.prevent="submit" />
        <button v-if="running" type="button" class="stop-btn" @click="stop">停止</button>
        <button v-else type="submit" :disabled="!draft.trim()">发送</button>
      </form>
    </section>

    <el-dialog v-model="profileOpen" title="风险画像" width="420px">
      <label v-for="field in PROFILE_FIELDS" :key="field.id" class="profile-field">
        {{ field.label }}
        <select v-if="field.options.length" v-model="profileDraft[field.id]">
          <option value="">未设置</option>
          <option v-for="option in field.options" :key="option" :value="option">{{ profileOptionLabel(field.id, option) }}</option>
        </select>
        <input v-else v-model="profileDraft[field.id]" type="number" min="0" step="0.01" />
      </label>
      <template #footer>
        <el-button @click="profileOpen = false">取消</el-button>
        <el-button type="primary" @click="submitProfile">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
/* 局部关键帧：Vue 会对 scoped 样式里的 keyframes 名称做作用域改名，
   因此这里必须本地定义（不能复用全局 style.css 中的同名关键帧）。 */
@keyframes riseIn {
  from {
    opacity: 0;
    transform: translateY(8px);
  }
  to {
    opacity: 1;
    transform: translateY(0);
  }
}
@keyframes pulse {
  0%,
  100% {
    opacity: 0.25;
    transform: translateY(0);
  }
  50% {
    opacity: 1;
    transform: translateY(-2px);
  }
}

.advisor { display: flex; min-height: 0; flex: 1; }

/* 会话侧栏 */
.sessions {
  display: flex;
  flex-direction: column;
  gap: 8px;
  width: 250px;
  padding: 16px;
  overflow: auto;
  border-right: 1px solid var(--color-border);
  background: var(--color-surface);
}
.new-chat {
  border: 0;
  border-radius: var(--radius);
  padding: 10px 12px;
  background: linear-gradient(150deg, var(--color-primary-light), var(--color-primary));
  color: #fff;
  font-weight: 600;
  cursor: pointer;
}
.new-chat:hover { filter: brightness(1.07); }
.session {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 8px;
  width: 100%;
  padding: 9px 10px;
  border: 1px solid var(--color-border);
  border-radius: var(--radius);
  background: transparent;
  font-size: 13px;
  text-align: left;
  cursor: pointer;
  transition: border-color 0.16s ease, background 0.16s ease;
}
.session:hover { background: var(--color-surface-alt); }
.session.active { border-color: var(--color-primary); background: var(--color-primary-soft); font-weight: 600; }
.session span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.session em { flex-shrink: 0; font-style: normal; color: var(--color-danger); font-size: 11px; }
.session em:hover { text-decoration: underline; }

/* 线程区 */
.thread { flex: 1; min-width: 0; display: flex; flex-direction: column; }
.thread-bar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 12px 18px;
  border-bottom: 1px solid var(--color-border);
  background: rgba(255, 254, 251, 0.5);
}
.stage {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  font-family: var(--font-mono);
  font-size: 12px;
  letter-spacing: 0.06em;
  color: var(--color-text-secondary);
}
.stage.running::before {
  content: '';
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--color-accent);
  animation: pulse 1.2s ease-in-out infinite;
}
.profile-entry {
  border: 1px solid var(--color-border);
  border-radius: 999px;
  padding: 7px 14px;
  background: var(--color-surface);
  color: var(--color-text-secondary);
  font-size: 12px;
  cursor: pointer;
}
.profile-entry:hover { border-color: var(--color-primary); color: var(--color-primary); }

/* 消息区 */
.messages {
  flex: 1;
  overflow: auto;
  padding: 20px 22px 28px;
  display: flex;
  flex-direction: column;
  gap: 14px;
}
.bubble {
  max-width: 780px;
  padding: 14px 16px;
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  background: var(--color-assistant-bubble);
  box-shadow: var(--shadow-sm);
  animation: riseIn 0.34s cubic-bezier(0.22, 0.61, 0.36, 1) both;
}
.bubble.assistant { align-self: flex-start; width: 100%; }
.bubble.user {
  align-self: flex-end;
  max-width: min(560px, 88%);
  border-color: transparent;
  border-bottom-right-radius: var(--radius-sm);
  background: linear-gradient(160deg, var(--color-primary-light), var(--color-primary));
  color: #fff;
  white-space: pre-wrap;
}
.user-text { margin: 0; }
.bubble-body { display: flex; flex-direction: column; gap: 12px; }
.markdown-pending { white-space: pre-wrap; }

/* 生成中占位 */
.generating {
  display: flex;
  align-items: center;
  gap: 8px;
  color: var(--color-text-muted);
  font-size: 13px;
}
.generating .dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--color-accent);
  animation: pulse 1.2s ease-in-out infinite;
}
.generating .dot:nth-child(2) { animation-delay: 0.15s; }
.generating .dot:nth-child(3) { animation-delay: 0.3s; }
.generating em { margin-left: 4px; font-style: normal; font-family: var(--font-mono); letter-spacing: 0.06em; }

/* 证据与提示分区 */
.evidence-block {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding-top: 12px;
  border-top: 1px dashed var(--color-hairline);
}
.evidence-block h4,
.notice-block h4 {
  margin: 0;
  font-family: var(--font-mono);
  font-size: 11px;
  letter-spacing: 0.12em;
  text-transform: uppercase;
  color: var(--color-text-muted);
}
.notice-block {
  max-width: 780px;
  padding: 10px 14px;
  border: 1px solid var(--color-border);
  border-left: 3px solid var(--color-accent);
  border-radius: var(--radius);
  background: var(--color-accent-soft);
}
.notice-block ul {
  margin: 6px 0 0;
  padding-left: 1.1em;
  color: var(--color-text-secondary);
  font-size: 13px;
}

.warning {
  margin: 0;
  color: var(--color-danger);
  font-size: 13px;
}
.warning button {
  margin-left: 8px;
  border: 1px solid var(--color-danger);
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--color-danger);
  padding: 3px 10px;
  cursor: pointer;
}

/* 中断表单 */
.resume {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 10px;
  padding: 16px 18px;
  border-top: 1px solid var(--color-border);
  background: var(--color-surface-alt);
}
.resume p { margin: 0; color: var(--color-text-secondary); font-size: 13px; }
.resume label { display: flex; flex-direction: column; gap: 4px; margin-bottom: 4px; font-size: 13px; }
.resume select,
.resume input {
  border: 1px solid var(--color-border);
  border-radius: var(--radius-sm);
  padding: 8px 10px;
  background: var(--color-surface);
}
.resume small { color: var(--color-text-muted); }
.resume button,
.composer button {
  border: 1px solid var(--color-primary);
  border-radius: var(--radius);
  background: var(--color-primary);
  color: #fff;
  padding: 9px 16px;
  font-weight: 600;
  cursor: pointer;
}
.resume button:disabled,
.composer button:disabled { opacity: 0.5; cursor: not-allowed; }

/* 输入区 */
.composer {
  display: flex;
  align-items: flex-end;
  gap: 10px;
  padding: 14px 18px;
  border-top: 1px solid var(--color-border);
  background: rgba(255, 254, 251, 0.65);
}
.composer textarea {
  flex: 1;
  min-height: 46px;
  max-height: 180px;
  resize: vertical;
  border: 1px solid var(--color-border);
  border-radius: var(--radius);
  padding: 10px 12px;
  font: inherit;
  background: var(--color-surface);
}
.composer textarea:focus { border-color: var(--color-primary); outline: none; }
.composer .stop-btn { background: transparent; color: var(--color-danger); border-color: var(--color-danger); }

.profile-field { display: flex; flex-direction: column; gap: 4px; margin-bottom: 10px; font-size: 13px; }
.profile-field select,
.profile-field input {
  border: 1px solid var(--color-border);
  border-radius: var(--radius-sm);
  padding: 8px 10px;
}
.hint { margin: 0 0 12px; color: var(--color-text-secondary); font-size: 12px; }

@media (prefers-reduced-motion: reduce) {
  .bubble { animation: none; }
  .stage.running::before,
  .generating .dot { animation: none; }
}

@media (max-width: 768px) {
  .advisor { flex-direction: column; }
  .sessions { width: auto; max-height: 180px; }
  .messages { padding: 16px 14px 22px; }
  .bubble { max-width: 100%; }
}
</style>
