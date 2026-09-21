import { flushPromises, mount } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'
import ElementPlus from 'element-plus'
import { buildTrendSeries } from '@/utils/evalTrend'
import TrendCard from '@/pages/eval/TrendCard.vue'
import { evalApi, type EvalRun } from '@/api/eval'

vi.mock('echarts/core', () => ({
  use: vi.fn(),
  init: vi.fn(() => ({ setOption: vi.fn(), dispose: vi.fn(), resize: vi.fn() })),
}))
vi.mock('echarts/charts', () => ({ LineChart: {} }))
vi.mock('echarts/components', () => ({
  TooltipComponent: {}, LegendComponent: {}, GridComponent: {},
}))
vi.mock('echarts/renderers', () => ({ CanvasRenderer: {} }))
vi.mock('@/api/eval', () => ({ evalApi: { listRuns: vi.fn() } }))

const mk = (id: number, status: EvalRun['status'], summary: EvalRun['summary'],
            at: string): EvalRun => ({
  id, kb_id: 3, kb_name: 'k', mode: 'retrieval', item_count: 2,
  summary, status, created_by: null, done_count: 2, created_at: at,
})

describe('buildTrendSeries', () => {
  it('keeps only completed runs with summary, asc by time', () => {
    const runs = [
      mk(3, 'running', null, '2026-09-21T12:00:00'),
      mk(2, 'completed', { hit: 0.6 }, '2026-09-21T11:00:00'),
      mk(1, 'completed', { hit: 0.8 }, '2026-09-21T10:00:00'),
      mk(4, 'failed', { hit: 0.1 }, '2026-09-21T13:00:00'),
    ]
    const out = buildTrendSeries(runs, ['hit'])
    // desc 输入 → 升序成线
    expect(out.times).toEqual(['2026-09-21T10:00:00', '2026-09-21T11:00:00'])
    expect(out.series).toEqual([{ name: 'hit', data: [0.8, 0.6] }])
  })

  it('missing metric values become null (connectNulls 补线)', () => {
    const runs = [
      mk(1, 'completed', { hit: 0.8 }, '2026-09-21T10:00:00'),
      mk(2, 'completed', { hit: null }, '2026-09-21T11:00:00'),
      mk(3, 'completed', { hit: 0.9 }, '2026-09-21T12:00:00'),
    ]
    const out = buildTrendSeries(runs, ['hit'])
    expect(out.series[0]!.data).toEqual([0.8, null, 0.9])
  })
})

describe('TrendCard', () => {
  it('renders chart after expand', async () => {
    vi.mocked(evalApi.listRuns).mockResolvedValue({
      total: 1, items: [mk(1, 'completed', { hit: 0.8 }, '2026-09-21T10:00:00')],
    })
    const w = mount(TrendCard, {
      props: { kbId: 3 },
      global: { plugins: [ElementPlus] },
    })
    await w.find('.trend-head').trigger('click')
    await flushPromises()
    expect(evalApi.listRuns).toHaveBeenCalledWith(
      expect.objectContaining({ kb_id: 3, mode: 'retrieval', page_size: 100 }))
    expect(w.find('.trend-canvas').exists()).toBe(true)
  })
})
