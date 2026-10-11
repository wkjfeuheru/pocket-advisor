/** 投顾 REST 与 POST SSE。SSE 使用 fetch，以便携带 Bearer token。 */

import { http } from '@/api/client'
import { getToken } from '@/app/session'
import { parseSseBuffer, type SseEvent } from './sse'

export interface SessionItem {
  id: string
  title: string
  created_at: string
  updated_at: string
}

export interface ChatMessage {
  id: string
  request_id: string
  role: string
  content: string
  created_at: string
  public_metadata?: {
    artifacts?: Record<string, unknown>[]
    candidate_stocks?: Record<string, unknown>[]
    warnings?: string[]
  }
}

export interface Profile {
  risk_level: string | null
  investment_horizon: string | null
  investment_goal: string | null
  liquidity_need: string | null
  budget_amount: number | null
}

export interface RunRecord {
  request_id: string
  session_id: string
  status: string
  graph_version?: string
  stage: string
  final_answer: string
  error_code: string
  retryable: boolean
  interrupt_payload: {
    missing_fields?: string[]
    profile?: Profile
    kind?: string
    reason?: string
    question?: string
    fields?: string[]
    options?: { id: string; label: string }[]
  } | null
}

export async function listSessions(): Promise<SessionItem[]> {
  const { data } = await http.get<SessionItem[]>('/agents/sessions')
  return data
}

export async function listMessages(sessionId: string): Promise<ChatMessage[]> {
  const { data } = await http.get<ChatMessage[]>(`/agents/sessions/${sessionId}/messages`)
  return data
}

export async function deleteSession(sessionId: string): Promise<void> {
  await http.delete(`/agents/sessions/${sessionId}`)
}

export async function getProfile(): Promise<Profile> {
  const { data } = await http.get<Profile>('/agents/profile')
  return data
}

export async function saveProfile(patch: Partial<Profile>): Promise<Profile> {
  const { data } = await http.patch<Profile>('/agents/profile', patch)
  return data
}

export async function getRun(requestId: string): Promise<RunRecord> {
  const { data } = await http.get<RunRecord>(`/agents/runs/${requestId}`)
  return data
}

export async function consumeSse(
  path: string,
  body: unknown,
  onEvent: (event: SseEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const token = getToken()
  const response = await fetch(`/api${path}`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Authorization: token ? `Bearer ${token}` : '',
    },
    body: JSON.stringify(body),
    signal,
  })
  if (!response.ok) {
    let detail = '请求失败'
    let code = ''
    try {
      const payload = await response.json()
      detail = typeof payload.detail === 'string' ? payload.detail : payload.detail?.message || detail
      code = payload.code || payload.detail?.code || ''
    } catch {
      detail = response.statusText || detail
    }
    const error = new Error(detail) as Error & { status?: number; code?: string }
    error.status = response.status
    error.code = code
    throw error
  }
  const reader = response.body?.getReader()
  if (!reader) throw new Error('响应没有正文')
  const decoder = new TextDecoder()
  let buffer = ''
  while (true) {
    const { value, done } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    const parsed = parseSseBuffer(buffer)
    buffer = parsed.rest
    for (const event of parsed.events) onEvent(event)
  }
  if (buffer.trim()) {
    const parsed = parseSseBuffer(`${buffer}\n\n`)
    for (const event of parsed.events) onEvent(event)
  }
}
