import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import ElementPlus, { ElSelect } from 'element-plus'
import EvalPage from '@/pages/EvalPage.vue'
import { evalApi, type EvalRun } from '@/api/eval'
import { kbApi } from '@/api/kb'

vi.mock('@/api/eval', () => ({
  evalApi: { listRuns: vi.fn(), getRun: vi.fn() },
}))
vi.mock('@/api/kb', () => ({ kbApi: { list: vi.fn() } }))

const runs: EvalRun[] = [
  {
    id: 7, kb_id: 3, kb_name: '手册库', mode: 'retrieval', item_count: 5,
    summary: { item_count: 5, hit: 0.8, mrr: 0.75, keyword_recall: 0.6 },
    created_at: '2026-09-21T10:00:00',
  },
  {
    id: 8, kb_id: 4, kb_name: null, mode: 'generation', item_count: 2,
    summary: { item_count: 2, faithfulness_avg: 0.9, relevancy_avg: 0.85, refused_count: 1 },
    created_at: '2026-09-21T11:00:00',
  },
]

const detail = {
  ...runs[0]!,
  items: [
    {
      id: 1, question: '问题0', expect_doc_ids: [1], expect_keywords: ['k'],
      answer: '答案甲', refused: false, hit_at_k: 1, mrr: 1, keyword_recall: 1,
      faithfulness: null, relevancy: null, reference_score: null,
    },
  ],
  items_truncated: false,
}

const mountPage = () => mount(EvalPage, { global: { plugins: [ElementPlus] } })

describe('EvalPage', () => {
  beforeEach(() => {
    vi.mocked(evalApi.listRuns).mockReset()
    vi.mocked(evalApi.getRun).mockReset()
    vi.mocked(kbApi.list).mockReset()
    vi.mocked(evalApi.listRuns).mockResolvedValue({ total: runs.length, items: runs })
    vi.mocked(kbApi.list).mockResolvedValue([
      { id: 3, name: '手册库' } as never,
    ])
  })

  it('renders run rows with kb name, deleted placeholder and default metrics', async () => {
    const w = mountPage()
    await flushPromises()
    expect(w.text()).toContain('手册库')
    expect(w.text()).toContain('(已删除)')
    expect(w.text()).toContain('0.80') // 默认列:命中率
    expect(w.text()).toContain('0.90') // 默认列:忠实度
  })

  it('shows CLI hint on empty state', async () => {
    vi.mocked(evalApi.listRuns).mockResolvedValue({ total: 0, items: [] })
    const w = mountPage()
    await flushPromises()
    expect(w.text()).toContain('暂无评估记录')
  })

  it('mode filter switches metric columns', async () => {
    const w = mountPage()
    await flushPromises()
    expect(w.text()).not.toContain('MRR')
    const modeSel = w
      .findAllComponents(ElSelect)
      .find((s) => s.props('placeholder') === '模式')!
    await modeSel.vm.$emit('update:modelValue', 'retrieval')
    await flushPromises()
    expect(w.text()).toContain('MRR')
    expect(w.text()).toContain('关键词召回')
    // lib < es2022(tsconfig.vitest),无 Array.prototype.at,用下标取最后一次调用
    const calls = vi.mocked(evalApi.listRuns).mock.calls
    const last = calls[calls.length - 1]?.[0]
    expect(last?.mode).toBe('retrieval')
  })

  it('clearing mode filter restores default metric columns', async () => {
    const w = mountPage()
    await flushPromises()
    const modeSel = w
      .findAllComponents(ElSelect)
      .find((s) => s.props('placeholder') === '模式')!
    await modeSel.vm.$emit('update:modelValue', 'retrieval')
    await flushPromises()
    expect(w.text()).toContain('MRR')
    // 清空后 Element Plus 把 v-model 置为 undefined:默认列回归,且重查不带 mode
    await modeSel.vm.$emit('update:modelValue', undefined)
    await flushPromises()
    expect(w.text()).toContain('命中率')
    expect(w.text()).toContain('忠实度')
    expect(w.text()).not.toContain('MRR')
    const calls = vi.mocked(evalApi.listRuns).mock.calls
    const last = calls[calls.length - 1]?.[0]
    expect(last?.mode).toBeUndefined()
  })

  it('403 from list shows inline forbidden alert', async () => {
    vi.mocked(evalApi.listRuns).mockRejectedValue({
      response: { status: 403 },
    })
    const w = mountPage()
    await flushPromises()
    expect(w.text()).toContain('仅库主/管理员可查看该库的评估记录')
  })

  it('row click loads run detail', async () => {
    vi.mocked(evalApi.getRun).mockResolvedValue(detail)
    const w = mountPage()
    await flushPromises()
    await w.find('.el-table__row').trigger('click')
    await flushPromises()
    expect(evalApi.getRun).toHaveBeenCalledWith(7)
    // el-drawer 默认 append-to-body=false,内容渲染在组件树内(wrapper 未挂到 document)
    expect(w.text()).toContain('文档1')
    expect(w.text()).toContain('关键词k')
  })
})
