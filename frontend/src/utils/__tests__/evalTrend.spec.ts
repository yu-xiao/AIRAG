import { flushPromises, mount } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'
import ElementPlus, { ElRadioGroup } from 'element-plus'
import { init } from 'echarts/core'
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

  it('switching mode resets metric selection to that mode defaults', async () => {
    vi.mocked(evalApi.listRuns).mockResolvedValue({
      total: 1,
      items: [mk(1, 'completed',
        { faithfulness_avg: 0.7, relevancy_avg: 0.8, reference_avg: 0.6 },
        '2026-09-21T10:00:00')],
    })
    const w = mount(TrendCard, {
      props: { kbId: 3 },
      global: { plugins: [ElementPlus] },
    })
    await w.find('.trend-head').trigger('click')
    await flushPromises()
    // 初始 retrieval 默认三键全选
    expect(w.findAll('.el-checkbox.is-checked').length).toBe(3)

    // 直接改 v-model(同 EvalRunsTab 用例模式):切到生成评估
    await w.findComponent(ElRadioGroup).vm.$emit('update:modelValue', 'generation')
    await flushPromises()

    // 重查带新 mode;勾选项重置为 generation 三项(旧 retrieval 键不残留)
    const calls = vi.mocked(evalApi.listRuns).mock.calls
    const last = calls[calls.length - 1]?.[0]
    expect(last?.mode).toBe('generation')
    expect(w.findAll('.el-checkbox').map((c) => c.text()))
      .toEqual(['忠实度', '相关性', '参考一致'])
    expect(w.findAll('.el-checkbox.is-checked').length).toBe(3)

    // 渲染路径:setOption 的 series 名经 labelOf 映射为 generation 标签,
    // 而非旧键残留 / 原始键名(faithfulness_avg)
    const results = vi.mocked(init).mock.results
    const { setOption } = results[results.length - 1]!.value as {
      setOption: ReturnType<typeof vi.fn>
    }
    const optCalls = setOption.mock.calls as unknown as Array<
      [{ series: { name: string }[] }, unknown]
    >
    const opt = optCalls[optCalls.length - 1]![0]
    expect(opt.series.map((s) => s.name))
      .toEqual(['忠实度', '相关性', '参考一致'])
  })
})
