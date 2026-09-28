/** 解析 fetch ReadableStream 里的 SSE，不使用浏览器 EventSource。 */

export interface SseEvent {
  event: string
  data: unknown
}

export function parseSseBuffer(buffer: string): { events: SseEvent[]; rest: string } {
  const normalized = buffer.replace(/\r\n/g, '\n')
  const parts = normalized.split('\n\n')
  const rest = normalized.endsWith('\n\n') ? '' : (parts.pop() ?? '')
  const events: SseEvent[] = []
  for (const part of parts) {
    if (!part.trim()) continue
    let event = 'message'
    const dataLines: string[] = []
    for (const line of part.split('\n')) {
      if (line.startsWith('event:')) event = line.slice(6).trim()
      else if (line.startsWith('data:')) dataLines.push(line.slice(5).trim())
    }
    const raw = dataLines.join('\n')
    let data: unknown = raw
    if (raw) {
      try {
        data = JSON.parse(raw)
      } catch {
        data = raw
      }
    }
    events.push({ event, data })
  }
  return { events, rest }
}
