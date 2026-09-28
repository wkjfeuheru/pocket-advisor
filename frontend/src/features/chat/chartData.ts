/** 把研究评分和技术指标整理成前端可画的数据结构。缺失维度不补造。 */

export interface ScoreBar {
  metric: string
  ratio: number
  display: string
}

export interface ScoreChart {
  label: string
  items: { code: string; bars: ScoreBar[] }[]
}

const SCORE_METRICS: { key: string; metric: string }[] = [
  { key: 'fundamental', metric: '基本面' },
  { key: 'technical', metric: '技术面' },
  { key: 'risk', metric: '风险' },
  { key: 'suitability', metric: '适配度' },
  { key: 'quality', metric: '质量' },
  { key: 'growth', metric: '成长' },
  { key: 'earnings_quality', metric: '盈利质量' },
  { key: 'momentum', metric: '动量' },
  { key: 'trend', metric: '趋势' },
  { key: 'valuation_inv', metric: '估值' },
  { key: 'stability', metric: '稳定性' },
  { key: 'liquidity', metric: '流动性' },
  { key: 'volatility_inv', metric: '波动' },
]

function clampRatio(value: number): number {
  return Math.max(0, Math.min(100, value))
}

export function buildScoreCompareChart(rows: any[] | null): ScoreChart | null {
  if (!rows?.length) return null
  const items = rows.map((row) => {
    const scores = row?.scores || row?.components || {}
    const code = row?.request?.stock_codes?.[0] || row?.thscode || row?.code || ''
    const bars = SCORE_METRICS.flatMap(({ key, metric }) => {
      const value = scores[key]
      if (value == null || Number.isNaN(Number(value))) return []
      const numeric = Number(value)
      return [{ metric, ratio: clampRatio(numeric), display: numeric.toFixed(1) }]
    })
    return { code, bars }
  }).filter((item) => item.bars.length)
  if (!items.length) return null
  return { label: items.length > 1 ? '多标的评分对比' : '', items }
}

export interface TechnicalSpec {
  code: string
  macdBars: { ratio: number }[]
  oscillator: { value: number; ratio: number; zones: { value: number }[] } | null
  summary: { trend?: string; latestPrice?: number }
}

export function buildTechnicalOverviewChart(input: Record<string, any> | null): TechnicalSpec[] {
  if (!input) return []
  const specs: TechnicalSpec[] = []
  for (const [code, payload] of Object.entries(input)) {
    const macd = payload?.MACD?.latest
    const rsi = payload?.RSI?.latest?.RSI6
    const dif = macd ? Number(macd.DIF) : null
    const dea = macd ? Number(macd.DEA) : null
    const hasMacd = dif != null && dea != null && !(dif === 0 && dea === 0) && !Number.isNaN(dif) && !Number.isNaN(dea)
    const hasRsi = rsi != null && !Number.isNaN(Number(rsi))
    if (!hasMacd && !hasRsi) continue
    let macdBars: { ratio: number }[] = []
    if (hasMacd && dif != null && dea != null) {
      const scale = Math.max(Math.abs(dif), Math.abs(dea)) || 1
      macdBars = [
        { ratio: Math.round((Math.abs(dif) / scale) * 100) },
        { ratio: Math.round((Math.abs(dea) / scale) * 100) },
      ]
    }
    specs.push({
      code,
      macdBars,
      oscillator: hasRsi
        ? { value: Number(rsi), ratio: clampRatio(Number(rsi)), zones: [{ value: 80 }, { value: 20 }] }
        : null,
      summary: {
        trend: payload?.summary?.trend,
        latestPrice: payload?.summary?.latest_price,
      },
    })
  }
  return specs
}

export function technicalsToChartInput(equities: Record<string, any>[] | null | undefined): Record<string, any> {
  const input: Record<string, any> = {}
  for (const item of equities || []) {
    const tech = item?.technicals
    if (!tech || tech.status !== 'ok') continue
    input[item.thscode] = {
      MACD: { latest: { DIF: tech.macd?.dif, DEA: tech.macd?.dea } },
      RSI: { latest: { RSI6: tech.rsi } },
      summary: { trend: tech.trend, latest_price: item.quote?.last },
    }
  }
  return input
}
