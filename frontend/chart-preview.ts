/** 临时预览入口：用假数据把新增图表组件渲染出来做视觉走查，验证完即删除。 */
import { createApp, h } from 'vue'
import '@/style.css'
import MeterBar from '@/shared/components/MeterBar.vue'
import SignedBar from '@/shared/components/SignedBar.vue'
import EquityResearchPanel from '@/features/chat/components/EquityResearchPanel.vue'
import MarketOverviewCards from '@/features/chat/components/MarketOverviewCards.vue'
import CandidatePoolTable from '@/features/chat/components/CandidatePoolTable.vue'

const equities = [
  {
    thscode: '600519.SH',
    name: '贵州茅台',
    as_of: '2026-09-27',
    quote: { last: 1680.5 },
    fundamentals: { roe: 31.2, gross_margin: 91.4 },
    components: { quality: 88, growth: 62, momentum: 74, valuation_inv: 41 },
    technicals: { status: 'ok', macd: { dif: 1.2, dea: 0.6 }, rsi: 58, trend: 'up' },
  },
  {
    thscode: '000001.SZ',
    name: '平安银行',
    as_of: '2026-09-27',
    quote: { last: 11.8 },
    fundamentals: { roe: 10.4, gross_margin: null },
    components: { quality: 61, growth: 45, stability: 77 },
    technicals: { status: 'ok', macd: { dif: 0.2, dea: 0.35 }, rsi: 34, trend: 'down' },
  },
]

const overview = {
  as_of: '2026-09-27',
  indices: [
    { thscode: '000001.SH', name: '上证综指', change_pct: 0.82 },
    { thscode: '399001.SZ', name: '深证成指', change_pct: -0.43 },
    { thscode: '399006.SZ', name: '创业板指', change_pct: 1.35 },
  ],
  breadth: { up: 2810, down: 1720, limit_up: 46, limit_down: 8, continue_board: 12, broken_board: 5 },
  sectors: {
    industry: [
      { name: '银行', change_pct: 1.62 },
      { name: '电子', change_pct: 2.41 },
      { name: '医药生物', change_pct: -0.88 },
    ],
    concept: [
      { name: '人工智能', change_pct: 3.24 },
      { name: '固态电池', change_pct: -1.12 },
    ],
  },
  risks: ['盘中行情可能过期', '部分行业缺少成交额数据'],
  warnings: ['样本仅覆盖主要指数'],
}

const screening = {
  candidates: [
    {
      thscode: '600519.SH',
      score: 88,
      components: { quality: 90, growth: 70, momentum: 82, valuation_inv: 55, liquidity: 78 },
      risks: ['估值偏高', '波动放大'],
      source: 'snapshot',
      as_of: '2026-09-26',
      snapshot_id: 'snap-1',
    },
    {
      thscode: '000001.SZ',
      score: 64,
      components: { quality: 60, growth: 48, stability: 79 },
      risks: [],
      source: 'snapshot',
      as_of: '2026-09-26',
      snapshot_id: 'snap-1',
    },
  ],
}

const Preview = {
  render() {
    return h('div', { class: 'page', style: 'display:flex;flex-direction:column;gap:24px' }, [
      h('p', { class: 'eyebrow' }, 'PREVIEW / CHART COMPONENTS'),
      h('section', { class: 'stat' }, [
        h('span', { class: 'stat-label' }, 'MeterBar'),
        h(MeterBar, { label: '质量', ratio: 0.88, display: '88.0' }),
        h(MeterBar, { label: '成长', ratio: 0.42, display: '42.0', tone: 'accent' }),
      ]),
      h('section', { class: 'stat' }, [
        h('span', { class: 'stat-label' }, 'SignedBar'),
        h(SignedBar, { label: '上证综指', value: 0.82, max: 1.35, display: '+0.82%' }),
        h(SignedBar, { label: '深证成指', value: -0.43, max: 1.35, display: '-0.43%' }),
      ]),
      h(EquityResearchPanel, { equities }),
      h(MarketOverviewCards, { overview }),
      h(CandidatePoolTable, { screening }),
    ])
  },
}

createApp(Preview).mount('#preview')
