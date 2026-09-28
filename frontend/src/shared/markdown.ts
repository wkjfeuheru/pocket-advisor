/** 安全 Markdown：禁用 raw HTML 与外链图片，支持表格和未闭合的流式文本。 */

import MarkdownIt from 'markdown-it'

const markdown = new MarkdownIt({
  html: false,
  linkify: false,
  breaks: false,
})

markdown.renderer.rules.image = (tokens, idx) => tokens[idx].content || ''

const HEADING = /^#{1,6}\s+\S/
const HR = /^(-{3,}|\*{3,}|_{3,})\s*$/
const LIST = /^([-*+]|\d+\.)\s+/
const TABLE = /^\|/

export interface RenderUnit {
  text: string
  complete: boolean
}

export interface StreamingRender {
  settled: string
  pending: string
}

function escapeHtml(value: string): string {
  return value
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
}

function isTable(source: string): boolean {
  const lines = source.trim().split('\n')
  return lines.length >= 2 && lines.every((line) => line.trim().startsWith('|')) && /^\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?$/.test(lines[1].trim())
}

function renderTable(source: string): string {
  const lines = source.trim().split('\n').map((line) => line.trim())
  const cells = (line: string) => line.replace(/^\|/, '').replace(/\|$/, '').split('|').map((cell) => cell.trim())
  const header = cells(lines[0]).map((cell) => `<th>${escapeHtml(cell)}</th>`).join('')
  const body = lines.slice(2).map((line) => {
    const row = cells(line).map((cell) => `<td>${escapeHtml(cell)}</td>`).join('')
    return `<tr>${row}</tr>`
  }).join('')
  return `<table><thead><tr>${header}</tr></thead><tbody>${body}</tbody></table>`
}

export function renderMarkdown(source: string): string {
  const text = source ?? ''
  if (!text.trim()) return ''
  if (isTable(text)) return renderTable(text)
  const withoutImages = text.replace(/!\[([^\]]*)\]\([^)]*\)/g, '$1')
  return markdown.render(withoutImages).trim()
}

function structural(line: string): boolean {
  return HEADING.test(line) || HR.test(line) || LIST.test(line) || TABLE.test(line)
}

export function splitRenderUnits(source: string): RenderUnit[] {
  if (!source) return []
  const endsWithNewline = source.endsWith('\n')
  const lines = source.split('\n')
  if (endsWithNewline) lines.pop()
  const units: { lines: string[]; closed: boolean; kind: 'mark' | 'paragraph' }[] = []
  for (const line of lines) {
    if (line === '') {
      if (units.length) units[units.length - 1].closed = true
      continue
    }
    const previous = units[units.length - 1]
    const kind = structural(line) ? 'mark' : 'paragraph'
    if (!previous || previous.closed || kind === 'mark' || previous.kind !== 'paragraph') {
      units.push({ lines: [line], closed: false, kind })
    } else {
      previous.lines.push(line)
    }
  }
  return units.map((unit, index) => {
    const text = unit.lines.join('\n')
    const lastLine = unit.lines[unit.lines.length - 1]
    const isLast = index === units.length - 1
    let complete = unit.closed || !isLast || endsWithNewline
    if (isLast && !endsWithNewline && (HEADING.test(lastLine) || HR.test(lastLine))) {
      complete = true
    }
    return { text, complete }
  })
}

export function renderMarkdownStreaming(source: string, finalize = false): StreamingRender {
  if (finalize) return { settled: renderMarkdown(source), pending: '' }
  const units = splitRenderUnits(source)
  const settled: string[] = []
  const pending: string[] = []
  for (const unit of units) {
    if (unit.complete) settled.push(renderMarkdown(unit.text))
    else pending.push(unit.text)
  }
  return { settled: settled.join(''), pending: pending.join('\n') }
}
