/** 投顾轮次的本地状态归并。画像字段只来自服务端事件或用户提交。 */

import type { SseEvent } from './sse'
import {
  allocationPlansFrom,
  isAllocationPayload,
  isClarificationPayload,
  type AllocationPlan,
  type ClarificationState,
} from './artifacts.ts'

export interface ProfileValues {
  risk_level?: string | null
  investment_horizon?: string | null
  investment_goal?: string | null
  liquidity_need?: string | null
  budget_amount?: number | null
}

export interface TurnState {
  status: 'idle' | 'running' | 'interrupted' | 'completed' | 'failed'
  stage: string
  answer: string
  warnings: string[]
  missingFields: string[]
  profile: ProfileValues
  artifacts: Record<string, any>[]
  candidateStocks: Record<string, any>[]
  clarification: ClarificationState | null
  allocationPlans: AllocationPlan[]
  retryable: boolean
  errorCode: string
  sessionId: string | null
  requestId: string | null
}

export interface RunSnapshot {
  request_id?: string
  session_id?: string
  status: string
  graph_version?: string
  stage?: string
  final_answer?: string
  retryable?: boolean
  error_code?: string
  interrupt_payload?: {
    missing_fields?: string[]
    profile?: ProfileValues
    kind?: string
    reason?: string
    question?: string
    fields?: string[]
    options?: { id: string; label: string }[]
    plans?: unknown[]
  } | null
}

export interface MessageState {
  id: string
  request_id: string
  role: string
}

const RISK_LEVEL_LABELS: Record<string, string> = {
  R1: '低风险',
  R2: '中低风险',
  R3: '中风险',
  R4: '中高风险',
  R5: '高风险',
}

/** 下拉展示文案。提交值仍是选项本身，例如风险等级提交 ``R1``。 */
export function profileOptionLabel(fieldId: string, value: string): string {
  const risk = fieldId === 'risk_level' ? RISK_LEVEL_LABELS[value] : ''
  return risk ? `${value} ${risk}` : value
}

export const PROFILE_FIELDS = [
  { id: 'risk_level', label: '风险等级', options: ['R1', 'R2', 'R3', 'R4', 'R5'] },
  { id: 'investment_horizon', label: '投资期限', options: ['短期', '中期', '长期'] },
  { id: 'investment_goal', label: '投资目标', options: ['保本', '收益', '均衡增长', '成长'] },
  { id: 'liquidity_need', label: '流动性', options: ['高', '中', '低'] },
  { id: 'budget_amount', label: '预算金额', options: [] as string[] },
] as const

export function initialTurn(): TurnState {
  return {
    status: 'idle',
    stage: '',
    answer: '',
    warnings: [],
    missingFields: [],
    profile: {},
    artifacts: [],
    candidateStocks: [],
    clarification: null,
    allocationPlans: [],
    retryable: false,
    errorCode: '',
    sessionId: null,
    requestId: null,
  }
}

/** 在发起网络请求前记录本轮标识，确保首个 SSE 事件前断流仍可恢复。 */
export function beginTurn(state: TurnState, requestId: string | null): TurnState {
  return {
    ...state,
    status: 'running',
    stage: '提交',
    answer: '',
    warnings: [],
    artifacts: [],
    candidateStocks: [],
    clarification: null,
    allocationPlans: [],
    retryable: false,
    errorCode: '',
    requestId,
  }
}

/** 服务端记录覆盖对应的乐观消息，未同步的本地消息继续保留。 */
export function mergeMessageHistory<T extends MessageState>(local: T[], server: T[]): T[] {
  const serverKeys = new Set(server.map((message) => `${message.request_id}:${message.role}`))
  const serverIds = new Set(server.map((message) => message.id))
  return [
    ...server,
    ...local.filter(
      (message) => !serverIds.has(message.id) && !serverKeys.has(`${message.request_id}:${message.role}`),
    ),
  ]
}

export function reduceTurn(state: TurnState, event: SseEvent): TurnState {
  const data = (event.data ?? {}) as Record<string, any>
  if (event.event === 'run') {
    return {
      ...state,
      status: 'running',
      sessionId: data.session_id ?? state.sessionId,
      requestId: data.request_id ?? state.requestId,
      answer: '',
      warnings: [],
      artifacts: [],
      candidateStocks: [],
      errorCode: '',
      retryable: false,
    }
  }
  if (event.event === 'status') {
    return {
      ...state,
      stage: data.stage || state.stage,
      retryable: Boolean(data.retryable),
      errorCode: data.error_code || state.errorCode,
    }
  }
  if (event.event === 'token') {
    return { ...state, answer: state.answer + String(data.text || '') }
  }
  if (event.event === 'warning') {
    return { ...state, warnings: [...state.warnings, String(data.message || '')] }
  }
  if (event.event === 'artifact') {
    return {
      ...state,
      artifacts: [...state.artifacts, data],
      candidateStocks: Array.isArray(data.candidate_stocks)
        ? data.candidate_stocks
        : state.candidateStocks,
    }
  }
  if (event.event === 'profile_required') {
    return {
      ...state,
      status: 'interrupted',
      missingFields: Array.isArray(data.missing_fields) ? data.missing_fields : [],
      profile: data.profile || {},
      clarification: null,
      allocationPlans: [],
    }
  }
  if (event.event === 'clarification_required') {
    return {
      ...state,
      status: 'interrupted',
      missingFields: [],
      allocationPlans: [],
      clarification: {
        kind: 'clarification',
        reason: String(data.reason || ''),
        question: String(data.question || ''),
        fields: Array.isArray(data.fields) ? data.fields : [],
        options: Array.isArray(data.options) ? data.options : [],
        request: data.request || {},
      },
    }
  }
  if (event.event === 'allocation_required') {
    // 方案列表只来自服务端声明；解析不出 plan_id 的条目会被丢弃。
    return {
      ...state,
      status: 'interrupted',
      missingFields: [],
      clarification: null,
      allocationPlans: allocationPlansFrom(data),
    }
  }
  if (event.event === 'done') {
    return { ...state, status: 'completed', answer: data.answer || state.answer, stage: '完成' }
  }
  if (event.event === 'error') {
    if (data.code === 'graph_version_changed') return versionChanged(state)
    return { ...state, status: 'failed', retryable: Boolean(data.retryable), errorCode: data.code || 'turn_failed' }
  }
  return state
}

/** 断流后按运行快照恢复界面。 */
export function applyRunSnapshot(state: TurnState, run: RunSnapshot): TurnState {
  const recovered = {
    ...state,
    requestId: run.request_id ?? state.requestId,
    sessionId: run.session_id ?? state.sessionId,
  }
  if (run.status === 'completed') {
    return {
      ...recovered,
      status: 'completed',
      stage: run.stage || '完成',
      answer: run.final_answer || state.answer,
      retryable: false,
    }
  }
  if (run.error_code === 'graph_version_changed') return versionChanged(recovered)
  if (run.status === 'failed') {
    return {
      ...recovered,
      status: 'failed',
      stage: run.stage || '失败',
      retryable: Boolean(run.retryable),
      errorCode: run.error_code || state.errorCode,
    }
  }
  if (run.status === 'interrupted') {
    const payload = run.interrupt_payload || {}
    if (isClarificationPayload(payload) || payload.kind === 'clarification') {
      return {
        ...recovered,
        status: 'interrupted',
        stage: run.stage || '等待澄清',
        missingFields: [],
        allocationPlans: [],
        clarification: {
          kind: 'clarification',
          reason: String(payload.reason || ''),
          question: String(payload.question || ''),
          fields: Array.isArray(payload.fields) ? payload.fields : [],
          options: Array.isArray(payload.options) ? payload.options : [],
          request: (payload as ClarificationState).request,
        },
        retryable: false,
      }
    }
    if (isAllocationPayload(payload)) {
      return {
        ...recovered,
        status: 'interrupted',
        stage: run.stage || '等待选择方案',
        missingFields: [],
        clarification: null,
        allocationPlans: allocationPlansFrom(payload),
        retryable: false,
      }
    }
    return {
      ...recovered,
      status: 'interrupted',
      stage: run.stage || '等待画像',
      missingFields: payload.missing_fields || [],
      profile: payload.profile || {},
      clarification: null,
      allocationPlans: [],
      retryable: false,
    }
  }
  return { ...recovered, status: 'running', stage: run.stage || run.status }
}

export function missingProfileFields(missing: string[]) {
  return PROFILE_FIELDS.filter((field) => missing.includes(field.id))
}

function versionChanged(state: TurnState): TurnState {
  return { ...state, status: 'failed', stage: '请重新发送', retryable: false,
    errorCode: 'graph_version_changed', missingFields: [], clarification: null, allocationPlans: [] }
}

export function turnErrorMessage(state: TurnState): string {
  return state.errorCode === 'graph_version_changed'
    ? '投顾已更新，请在输入框重新发送问题。' : '本轮没有完成。'
}

export function newConversationState(): TurnState {
  return initialTurn()
}
