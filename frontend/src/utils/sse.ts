export interface SSEEvent {
  type: 'token' | 'citations' | 'done' | 'error'
  data: unknown
}

export function parseSSEChunk(raw: string): SSEEvent[] {
  const events: SSEEvent[] = []
  const frames = raw.split('\n\n')
  for (const frame of frames) {
    const line = frame.split('\n').find((l) => l.startsWith('data: '))
    if (!line) continue
    try {
      events.push(JSON.parse(line.slice(6)))
    } catch {
      // 坏帧跳过(半包尾部由调用方缓冲)
    }
  }
  return events
}
