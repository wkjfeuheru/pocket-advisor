import { test } from 'node:test'
import assert from 'node:assert/strict'

import { clarifyStreamPath, isClarificationPayload, marketOverviewView, optionsForField, primaryStockArtifact, publicArtifacts } from '../src/features/chat/artifacts.ts'
import { buildScoreCompareChart, technicalsToChartInput } from '../src/features/chat/chartData.ts'
import * as advisorState from '../src/features/chat/state.ts'

const { applyRunSnapshot, initialTurn, reduceTurn } = advisorState

test('clarification_required only keeps declared fields and options', () => {
  const state = reduceTurn(initialTurn(), {
    event: 'clarification_required',
    data: {
      kind: 'clarification',
      reason: 'ambiguous_symbol',
      question: '请选择标的',
      fields: ['symbols'],
      options: [{ id: '000001.SZ', label: '平安银行 000001.SZ' }],
    },
  })
  assert.equal(state.status, 'interrupted')
  assert.deepEqual(state.clarification?.fields, ['symbols'])
  assert.deepEqual(state.clarification?.options.map((item) => item.id), ['000001.SZ'])
  assert.equal(state.missingFields.length, 0)
  assert.equal(clarifyStreamPath('req-1'), '/agents/runs/req-1/clarify/stream')
})

test('market overview artifact renders index breadth fields without inventing rows', () => {
  const artifact = primaryStockArtifact([{
    scenario: 'market_overview',
    market_overview: {
      indices: [{ thscode: '000001.SH', name: '上证综指', change_pct: 0.8 }],
      breadth: { up: 2000, down: 1000, limit_up: 12 },
    },
  }])
  assert.equal(artifact?.scenario, 'market_overview')
  assert.equal(artifact?.market_overview.indices.length, 1)
  assert.equal(publicArtifacts(null).length, 0)
})

test('equity research score bars skip missing dimensions', () => {
  const chart = buildScoreCompareChart([
    { thscode: '600519.SH', components: { quality: 80, growth: 40 } },
  ])
  assert.equal(chart?.items[0].code, '600519.SH')
  assert.deepEqual(chart?.items[0].bars.map((item) => item.metric), ['质量', '成长'])
  const technicals = technicalsToChartInput([
    { thscode: '600519.SH', technicals: { status: 'ok', macd: { dif: 1, dea: 0.5 }, rsi: 55, trend: 'up' } },
    { thscode: '000001.SZ', technicals: { status: 'unavailable' } },
  ])
  assert.ok(technicals['600519.SH'])
  assert.equal(technicals['000001.SZ'], undefined)
})

test('screening artifact keeps snapshot provenance columns', () => {
  const artifact = primaryStockArtifact([{
    scenario: 'stock_screening',
    stock_screening: {
      candidates: [{
        thscode: 'AAA.SH',
        score: 90,
        components: { quality: 70 },
        risks: ['波动偏高'],
        source: 'snapshot',
        as_of: '2026-09-26',
        snapshot_id: 'snap-1',
      }],
    },
  }])
  const row = artifact?.stock_screening.candidates[0]
  assert.equal(row.thscode, 'AAA.SH')
  assert.equal(row.snapshot_id, 'snap-1')
  assert.equal(row.as_of, '2026-09-26')
})

test('clarification options and market cards stay on their own fields', () => {
  const options = [
    { id: 'value_quality', label: '价值质量', field: 'strategy' },
    { id: 'short', label: '短线', field: 'horizon' },
  ]
  assert.deepEqual(optionsForField(options, 'strategy').map((item) => item.id), ['value_quality'])
  assert.deepEqual(optionsForField(options, 'horizon').map((item) => item.id), ['short'])
  const view = marketOverviewView({
    sectors: {
      industry: [{ name: '银行', change_pct: 2.1 }],
      concept: [{ name: '人工智能', change_pct: 3.2 }],
    },
    risks: ['波动偏高'],
    warnings: ['盘中行情过期'],
  })
  assert.equal(view.industries[0].name, '银行')
  assert.equal(view.concepts[0].name, '人工智能')
  assert.deepEqual(view.risks, ['波动偏高', '盘中行情过期'])
})

test('history without artifacts stays markdown-only and interrupt restores clarification form', () => {
  assert.equal(primaryStockArtifact([]), null)
  const paused = applyRunSnapshot(initialTurn(), {
    status: 'interrupted',
    stage: '等待澄清',
    interrupt_payload: {
      kind: 'clarification',
      reason: 'missing_strategy',
      question: '请选择策略',
      fields: ['strategy'],
      options: [{ id: 'value_quality', label: '价值质量' }],
    },
  })
  assert.equal(paused.clarification?.reason, 'missing_strategy')
  assert.ok(isClarificationPayload(paused.clarification))
  const profilePaused = applyRunSnapshot(initialTurn(), {
    status: 'interrupted',
    stage: '等待画像',
    interrupt_payload: { missing_fields: ['risk_level'], profile: {} },
  })
  assert.equal(profilePaused.clarification, null)
  assert.deepEqual(profilePaused.missingFields, ['risk_level'])
})
