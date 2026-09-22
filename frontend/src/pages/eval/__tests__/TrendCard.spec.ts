import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import ElementPlus from 'element-plus'
import TrendCard from '@/pages/eval/TrendCard.vue'
import { evalApi } from '@/api/eval'

vi.mock('@/api/eval', () => ({ evalApi: { listRuns: vi.fn() } }))

const fakeChart = vi.hoisted(() => ({
  setOption: vi.fn(), dispose: vi.fn(), resize: vi.fn(),
}))
const initMock = vi.hoisted(() => vi.fn(() => fakeChart))
vi.mock('echarts/core', () => ({ use: vi.fn(), init: initMock }))
// vitest 4 工厂 mock 缺命名导出会在组件取值时抛错,须补齐组件静态导入的名称
vi.mock('echarts/renderers', () => ({ CanvasRenderer: vi.fn() }))
vi.mock('echarts/charts', () => ({ LineChart: vi.fn() }))
vi.mock('echarts/components', () => ({
  GridComponent: vi.fn(), LegendComponent: vi.fn(), TooltipComponent: vi.fn(),
}))

class RO {
  static instances: RO[] = []
  cb: ResizeObserverCallback
  observe = vi.fn()
  disconnect = vi.fn()
  unobserve = vi.fn()
  constructor(cb: ResizeObserverCallback) {
    this.cb = cb
    RO.instances.push(this)
  }
}

const runWithSummary = {
  id: 1, kb_id: 3, kb_name: 'k', mode: 'retrieval', item_count: 3,
  summary: { item_count: 3, hit: 0.5, mrr: 0.4, keyword_recall: 0.6 },
  status: 'completed', created_by: 'ed', done_count: 3,
  created_at: '2026-09-21T10:00:00',
}

describe('TrendCard', () => {
  beforeEach(() => {
    vi.mocked(evalApi.listRuns).mockReset()
    vi.mocked(evalApi.listRuns).mockResolvedValue({ total: 1, items: [runWithSummary as never] })
    RO.instances.length = 0
    fakeChart.setOption.mockClear()
    fakeChart.resize.mockClear()
    initMock.mockClear()
    vi.stubGlobal('ResizeObserver', RO)
  })
  afterEach(() => vi.unstubAllGlobals())

  it('shows empty state when kb selected but no completed runs', async () => {
    vi.mocked(evalApi.listRuns).mockResolvedValue({ total: 0, items: [] })
    const w = mount(TrendCard, {
      props: { kbId: 3 }, global: { plugins: [ElementPlus] },
    })
    await w.find('.trend-head').trigger('click')
    await flushPromises()
    expect(w.text()).toContain('暂无已完成的运行')
    expect(w.find('.trend-canvas').exists()).toBe(false)
    expect(initMock).not.toHaveBeenCalled()
  })

  it('renders chart when data exists and resizes via ResizeObserver', async () => {
    const w = mount(TrendCard, {
      props: { kbId: 3 }, global: { plugins: [ElementPlus] },
    })
    await w.find('.trend-head').trigger('click')
    await flushPromises()
    expect(w.find('.trend-canvas').exists()).toBe(true)
    expect(initMock).toHaveBeenCalledTimes(1)
    expect(fakeChart.setOption).toHaveBeenCalledTimes(1)
    // resize 接线:观察根元素;容器尺寸变化回调 → chart.resize()
    expect(RO.instances.length).toBe(1)
    expect(RO.instances[0]!.observe).toHaveBeenCalled()
    RO.instances[0]!.cb([], {} as never)
    expect(fakeChart.resize).toHaveBeenCalledTimes(1)
  })
})
