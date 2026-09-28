import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  actionLabel,
  allocationAdoptPath,
  allocationPlansFrom,
  formatAmount,
  formatPercent,
  isAllocationPayload,
} from '../src/features/chat/artifacts.ts'
import * as advisorState from '../src/features/chat/state.ts'

const { applyRunSnapshot, beginTurn, initialTurn, reduceTurn } = advisorState

const PLAN_PAYLOAD = {
  kind: 'allocation',
  agent_version: 'portfolio-allocation-v1',
  plans: [
    {
      plan_id: 'p-light',
      tier: 'light',
      label: '微调',
      turnover_rate: 0.0625,
      estimated_fee: 49.375,
      items: [
        {
          product_code: '110011',
          action: 'reduce',
          current_weight: 0.8,
          target_weight: 0.7125,
          delta_weight: -0.0875,
          reason: '降低单一主动基金暴露',
        },
      ],
      limitations: ['净值截至 2026-09-28'],
    },
  ],
}

test('allocation_required puts the turn into an interrupted state with plans', () => {
  const state = reduceTurn(initialTurn(), { event: 'allocation_required', data: PLAN_PAYLOAD })

  assert.equal(state.status, 'interrupted')
  assert.equal(state.allocationPlans.length, 1)
  assert.equal(state.allocationPlans[0].plan_id, 'p-light')
  assert.equal(state.allocationPlans[0].label, '微调')
  assert.equal(state.clarification, null)
  assert.equal(state.missingFields.length, 0)
  assert.equal(allocationAdoptPath('req-1'), '/agents/runs/req-1/allocation/stream')
})

test('a plan without a plan_id is dropped instead of rendering an unusable button', () => {
  const plans = allocationPlansFrom({
    kind: 'allocation',
    plans: [{ tier: 'light', label: '微调' }, { plan_id: 'ok', tier: 'moderate', label: '中度' }],
  })

  assert.equal(plans.length, 1)
  assert.equal(plans[0].plan_id, 'ok')
})

test('a malformed payload yields an empty plan list rather than throwing', () => {
  for (const bad of [null, undefined, {}, { plans: 'nope' }, { plans: [null, 1] }]) {
    assert.deepEqual(allocationPlansFrom(bad), [])
  }
})

test('isAllocationPayload distinguishes allocation from clarification and profile', () => {
  assert.equal(isAllocationPayload(PLAN_PAYLOAD), true)
  assert.equal(isAllocationPayload({ kind: 'clarification', plans: [] }), false)
  assert.equal(isAllocationPayload({ missing_fields: ['risk_level'] }), false)
})

test('switching interrupt kinds clears the other kind of pending state', () => {
  // 先挂起在方案选择上
  const allocation = reduceTurn(initialTurn(), { event: 'allocation_required', data: PLAN_PAYLOAD })
  assert.ok(allocation.allocationPlans.length)

  // 再收到澄清事件：方案列表必须清空，避免两个表单同时出现
  const clarification = reduceTurn(allocation, {
    event: 'clarification_required',
    data: { kind: 'clarification', reason: 'x', question: 'q', fields: ['strategy'], options: [] },
  })
  assert.deepEqual(clarification.allocationPlans, [])
  assert.ok(clarification.clarification)

  // 反过来也要清
  const back = reduceTurn(clarification, { event: 'allocation_required', data: PLAN_PAYLOAD })
  assert.equal(back.clarification, null)
  assert.equal(back.allocationPlans.length, 1)
})

test('a refreshed allocation interrupt restores the plan cards', () => {
  const restored = applyRunSnapshot(initialTurn(), {
    status: 'interrupted',
    stage: '等待选择方案',
    interrupt_payload: PLAN_PAYLOAD,
  })

  assert.equal(restored.status, 'interrupted')
  assert.equal(restored.stage, '等待选择方案')
  assert.equal(restored.allocationPlans.length, 1)
  assert.equal(restored.allocationPlans[0].plan_id, 'p-light')
  assert.equal(restored.clarification, null)
  assert.deepEqual(restored.missingFields, [])
})

test('starting a new turn clears any previous pending plans', () => {
  const allocation = reduceTurn(initialTurn(), { event: 'allocation_required', data: PLAN_PAYLOAD })

  const fresh = beginTurn(allocation, 'req-2')

  assert.deepEqual(fresh.allocationPlans, [])
  assert.equal(fresh.status, 'running')
})

test('action and number formatting match the backend vocabulary', () => {
  assert.equal(actionLabel('open'), '新开')
  assert.equal(actionLabel('add'), '加仓')
  assert.equal(actionLabel('reduce'), '减仓')
  assert.equal(actionLabel('close'), '清仓')
  assert.equal(actionLabel('hold'), '持有')
  // 未知动作原样返回，不猜测
  assert.equal(actionLabel('weird'), 'weird')

  assert.equal(formatPercent(0.0625), '6.25%')
  assert.equal(formatPercent(0.3, 1), '30.0%')
  assert.equal(formatAmount(49.375), '49.38')
  // 非有限数不能渲染成 NaN
  assert.equal(formatPercent(Number.NaN), '—')
  assert.equal(formatAmount(Number.POSITIVE_INFINITY), '—')
})
