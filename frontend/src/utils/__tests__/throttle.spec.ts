import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { throttle } from '@/utils/throttle'

describe('throttle', () => {
  beforeEach(() => {
    vi.useFakeTimers()
  })
  afterEach(() => {
    vi.useRealTimers()
  })

  it('coalesces bursts into fewer calls with trailing fire', () => {
    const fn = vi.fn<(arg: number) => void>()
    const t = throttle(fn, 120)
    for (let i = 0; i < 50; i++) t(i) // 一帧内 50 次
    expect(fn.mock.calls.length).toBeLessThanOrEqual(2) // 首次 + 至多一次尾随
    vi.advanceTimersByTime(200)
    expect(fn.mock.calls.length).toBeLessThanOrEqual(2)
  })

  it('fires again after the window passes', () => {
    const fn = vi.fn<(arg: number) => void>()
    const t = throttle(fn, 100)
    t(1)
    vi.advanceTimersByTime(150)
    t(2)
    expect(fn).toHaveBeenCalledTimes(2)
    expect(fn).toHaveBeenLastCalledWith(2)
  })
})
