import { describe, expect, it } from 'vitest'
import { parseSSEChunk } from '@/utils/sse'

describe('parseSSEChunk', () => {
  it('parses data frames and skips bad json', () => {
    const raw = 'data: {"type":"token","data":"你好"}\n\n junk \n\ndata: {bad}\n\n'
    const events = parseSSEChunk(raw)
    expect(events).toEqual([{ type: 'token', data: '你好' }])
  })

  it('handles multiline raw buffer tail', () => {
    const raw = 'data: {"type":"done","data":{"conversation_id":7}}\n\ndata: {"type":"tok'
    const events = parseSSEChunk(raw)
    expect(events).toHaveLength(1)
    expect(events[0]?.type).toBe('done')
  })
})
