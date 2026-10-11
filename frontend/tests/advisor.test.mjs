import { test } from 'node:test'
import assert from 'node:assert/strict'

import { parseSseBuffer } from '../src/features/chat/sse.ts'
import * as advisorState from '../src/features/chat/state.ts'

const { applyRunSnapshot, initialTurn, missingProfileFields, profileOptionLabel, reduceTurn } = advisorState

test('版本变化清除旧中断，禁用重试并提示从输入框重新发送', () => {
  const paused = { ...initialTurn(), status: 'interrupted', missingFields: ['risk_level'], clarification: { kind: 'clarification' }, allocationPlans: [{ plan_id: 'old' }] }
  const failed = applyRunSnapshot(paused, { status: 'failed', error_code: 'graph_version_changed', retryable: false })
  assert.equal(failed.status, 'failed')
  assert.equal(failed.retryable, false)
  assert.equal(failed.errorCode, 'graph_version_changed')
  assert.deepEqual(failed.missingFields, [])
  assert.equal(failed.clarification, null)
  assert.deepEqual(failed.allocationPlans, [])
  assert.equal(advisorState.turnErrorMessage(failed), '投顾已更新，请在输入框重新发送问题。')
  const event = reduceTurn(paused, { event: 'error', data: { code: 'graph_version_changed', retryable: true } })
  assert.equal(event.retryable, false)
  assert.equal(event.clarification, null)
})

test('确定性失败快照保持失败状态而不会重新锁住输入框', () => {
  const failed = applyRunSnapshot(initialTurn(), { status: 'failed', retryable: false, error_code: 'invalid_input' })
  assert.equal(failed.status, 'failed')
  assert.equal(failed.retryable, false)
  assert.equal(failed.errorCode, 'invalid_input')
})

test('SSE 解析保留半包并还原事件', () => {
  const first = parseSseBuffer('event: token\ndata: {"text":"你"}\n\nevent: to')
  assert.equal(first.events.length, 1)
  assert.equal(first.events[0].event, 'token')
  assert.equal(first.events[0].data.text, '你')
  assert.equal(first.rest, 'event: to')
  const second = parseSseBuffer(`${first.rest}ken\ndata: {"text":"好"}\n\n`)
  assert.equal(second.events[0].data.text, '好')
  assert.equal(second.rest, '')
})

test('事件按顺序归并，专家字段不会进入答案以外的状态', () => {
  let state = initialTurn()
  for (const event of [
    { event: 'run', data: { request_id: 'r1', session_id: 's1' } },
    { event: 'status', data: { stage: '识别问题' } },
    { event: 'token', data: { text: '最终' } },
    { event: 'warning', data: { message: '行情暂不可用' } },
    { event: 'done', data: { answer: '最终答案' } },
  ]) {
    state = reduceTurn(state, event)
  }
  assert.equal(state.status, 'completed')
  assert.equal(state.answer, '最终答案')
  assert.equal(state.stage, '完成')
  assert.deepEqual(state.warnings, ['行情暂不可用'])
  assert.equal(state.sessionId, 's1')
})

test('风险等级选项带中文说明，提交值仍是等级代码', () => {
  assert.equal(profileOptionLabel('risk_level', 'R1'), 'R1 低风险')
  assert.equal(profileOptionLabel('risk_level', 'R2'), 'R2 中低风险')
  assert.equal(profileOptionLabel('risk_level', 'R3'), 'R3 中风险')
  assert.equal(profileOptionLabel('risk_level', 'R4'), 'R4 中高风险')
  assert.equal(profileOptionLabel('risk_level', 'R5'), 'R5 高风险')
  assert.equal(profileOptionLabel('liquidity_need', '高'), '高')
})

test('画像恢复只暴露本轮缺失字段', () => {
  const state = reduceTurn(initialTurn(), {
    event: 'profile_required',
    data: {
      missing_fields: ['risk_level', 'budget_amount'],
      profile: { risk_level: null, investment_horizon: '中期' },
    },
  })
  assert.equal(state.status, 'interrupted')
  assert.deepEqual(missingProfileFields(state.missingFields).map((field) => field.id), ['risk_level', 'budget_amount'])
  assert.equal(state.profile.investment_horizon, '中期')
})

test('断流后按完成、可重试和中断分别恢复', () => {
  const base = { ...initialTurn(), requestId: 'r1', status: 'running' }
  const done = applyRunSnapshot(base, {
    request_id: 'r1',
    session_id: 's1',
    status: 'completed',
    stage: '完成',
    final_answer: '已完成',
  })
  assert.equal(done.status, 'completed')
  assert.equal(done.answer, '已完成')
  assert.equal(done.sessionId, 's1')
  assert.equal(done.stage, '完成')
  const retry = applyRunSnapshot(base, { status: 'failed', stage: '失败', retryable: true, error_code: 'turn_failed' })
  assert.equal(retry.status, 'failed')
  assert.equal(retry.retryable, true)
  assert.equal(retry.stage, '失败')
  const paused = applyRunSnapshot(base, {
    status: 'interrupted',
    stage: '等待画像',
    interrupt_payload: { missing_fields: ['liquidity_need'], profile: {} },
  })
  assert.equal(paused.status, 'interrupted')
  assert.deepEqual(paused.missingFields, ['liquidity_need'])
  assert.equal(paused.stage, '等待画像')
  assert.equal(paused.clarification, null)

  const running = applyRunSnapshot(base, { status: 'running', stage: '核对适配性' })
  assert.equal(running.stage, '核对适配性')
})

test('提交后在首个 SSE 事件前保存 request_id 以供断流恢复', () => {
  const state = advisorState.beginTurn(initialTurn(), 'request-before-stream')

  assert.equal(state.status, 'running')
  assert.equal(state.stage, '提交')
  assert.equal(state.requestId, 'request-before-stream')
})

test('服务端历史合并 assistant 回复且保留尚未同步的本地消息', () => {
  const local = [
    { id: 'user-1', request_id: 'r1', role: 'user', content: '问题一', created_at: '' },
    { id: 'user-2', request_id: 'r2', role: 'user', content: '问题二', created_at: '' },
  ]
  const server = [
    { id: 'stored-user-1', request_id: 'r1', role: 'user', content: '问题一', created_at: '2026-09-27T08:00:00Z' },
    { id: 'assistant-1', request_id: 'r1', role: 'assistant', content: '回答一', created_at: '2026-09-27T08:00:01Z' },
  ]

  const merged = advisorState.mergeMessageHistory(local, server)

  assert.deepEqual(merged.map((message) => message.id), ['stored-user-1', 'assistant-1', 'user-2'])
  assert.equal(merged[0].created_at, '2026-09-27T08:00:00Z')
  assert.equal(merged[1].content, '回答一')
})
