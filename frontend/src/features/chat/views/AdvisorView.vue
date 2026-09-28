<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { renderMarkdownStreaming } from '@/shared/markdown'
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
  type TurnState,
} from '../state'

const sessions = ref<SessionItem[]>([])
const selectedId = ref<string | null>(null)
const history = ref<ChatMessage[]>([])
const draft = ref('')
const turn = ref<TurnState>(initialTurn())
const profile = ref<Profile | null>(null)
const profileOpen = ref(false)
const profileDraft = ref<Record<string, string>>({})
const resumeDraft = ref<Record<string, string>>({})
const clarifyDraft = ref<Record<string, string>>({})

const running = computed(() => turn.value.status === 'running')
const rendered = computed(() => renderMarkdownStreaming(turn.value.answer, turn.value.status !== 'running'))
const promptedFields = computed(() => missingProfileFields(turn.value.missingFields))

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

async function send(path: string, body: unknown, requestId: string | null = turn.value.requestId) {
  if (running.value) return
  turn.value = beginTurn(turn.value, requestId)
  try {
    await consumeSse(path, body, onEvent)
  } catch (error: any) {
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
    const value = (resumeDraft.value[field.id] || '').trim()
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
    const value = (profileDraft.value[field.id] || '').trim()
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

onMounted(async () => {
  await refreshSessions()
  profile.value = await getProfile()
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
        <span class="stage">{{ turn.stage || (running ? '处理中' : '待命') }}</span>
        <button type="button" class="profile-entry" @click="openProfile">风险画像</button>
      </header>

      <div class="messages">
        <article v-for="item in history" :key="item.id" class="bubble" :class="item.role">
          <template v-if="item.content">{{ item.content }}</template>
          <StockArtifacts v-if="item.role === 'assistant'" :artifacts="item.public_metadata?.artifacts" />
        </article>
        <article v-if="turn.answer || running || turn.artifacts.length" class="bubble assistant">
          <div v-if="rendered.settled" class="markdown" v-html="rendered.settled"></div>
          <span v-if="rendered.pending">{{ rendered.pending }}</span>
          <StockArtifacts :artifacts="turn.artifacts" />
        </article>
        <p v-for="warning in turn.warnings" :key="warning" class="warning">{{ warning }}</p>
        <p v-if="turn.status === 'failed'" class="warning">
          本轮没有完成。
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
        <button type="submit" :disabled="running || !draft.trim()">发送</button>
      </form>
    </section>

    <el-dialog v-model="profileOpen" title="风险画像" width="420px">
      <p class="hint">只有点击保存才会写入长期画像。</p>
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
.advisor { display: flex; min-height: 0; flex: 1; }
.sessions { width: 240px; border-right: 1px solid var(--color-border); padding: 16px; overflow: auto; background: var(--color-surface); }
.new-chat, .profile-entry, .composer button, .resume button { border: 1px solid var(--color-primary); background: var(--color-primary); color: #fff; padding: 8px 12px; cursor: pointer; }
.session { display: flex; justify-content: space-between; width: 100%; margin-top: 8px; padding: 8px; border: 1px solid var(--color-border); background: transparent; text-align: left; cursor: pointer; }
.session.active { border-color: var(--color-primary); }
.session em { font-style: normal; color: var(--color-danger); }
.thread { flex: 1; min-width: 0; display: flex; flex-direction: column; }
.thread-bar { display: flex; justify-content: space-between; align-items: center; padding: 12px 16px; border-bottom: 1px solid var(--color-border); }
.stage { font-family: var(--font-mono); color: var(--color-text-secondary); }
.messages { flex: 1; overflow: auto; padding: 16px; display: flex; flex-direction: column; gap: 12px; }
.bubble { max-width: 720px; padding: 12px 14px; border: 1px solid var(--color-border); background: var(--color-assistant-bubble); white-space: pre-wrap; }
.bubble.user { align-self: flex-end; background: var(--color-user-bubble); color: #fff; }
.warning { color: var(--color-danger); margin: 0; }
.composer, .resume { display: flex; gap: 8px; padding: 12px 16px; border-top: 1px solid var(--color-border); }
.composer textarea { flex: 1; resize: vertical; border: 1px solid var(--color-border); padding: 8px; font: inherit; }
.resume { flex-direction: column; align-items: flex-start; }
.profile-field, .resume label { display: flex; flex-direction: column; gap: 4px; margin-bottom: 8px; }
.hint { color: var(--color-text-secondary); }
@media (max-width: 768px) {
  .advisor { flex-direction: column; }
  .sessions { width: auto; max-height: 180px; }
}
</style>
