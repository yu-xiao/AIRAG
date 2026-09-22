import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import ElementPlus from 'element-plus'
import CompareDrawer from '@/pages/eval/CompareDrawer.vue'
import { evalApi, type EvalItem, type EvalRunDetail } from '@/api/eval'

vi.mock('@/api/eval', () => ({ evalApi: { getRun: vi.fn() } }))

const item = (over: Partial<EvalItem>): EvalItem => ({
  id: 1, question: 'q', expect_doc_ids: [1], expect_keywords: ['k'],
  answer: 'a', refused: false, hit_at_k: 1, mrr: 1, keyword_recall: 1,
  faithfulness: null, relevancy: null, reference_score: null,
  ...over,
})

const detailOf = (id: number, items: EvalItem[]): EvalRunDetail => ({
  id, kb_id: 3, kb_name: '手册库', mode: 'retrieval', item_count: items.length,
  summary: { item_count: items.length, hit: 0.5, mrr: 0.5, keyword_recall: 0.5 },
  status: 'completed', created_by: 'ed', done_count: items.length,
  created_at: '2026-09-21T10:00:00', items, items_truncated: false, error: null,
})

describe('CompareDrawer', () => {
  beforeEach(() => {
    vi.mocked(evalApi.getRun).mockReset()
  })

  it('item grid shows em-dash for legacy zero rows, red 0.00 for measured zero', async () => {
    vi.mocked(evalApi.getRun)
      .mockResolvedValueOnce(detailOf(1, [
        // 历史行:A1 前空期望按 0 落库 → 逐题格应显「—」且不标红
        item({ id: 1, question: '历史题', expect_doc_ids: null, expect_keywords: null, hit_at_k: 0, mrr: 0, keyword_recall: 0 }),
        // 真测量行:设了期望的 0 → 红 0.00
        item({ id: 2, question: '测量题', hit_at_k: 0, mrr: 0, keyword_recall: 0 }),
      ]))
      .mockResolvedValueOnce(detailOf(2, [
        // B 侧同题配对(question 是 joinItems 配对键),给健康分值避免干扰 A 侧断言
        item({ id: 3, question: '历史题' }),
        item({ id: 4, question: '测量题' }),
      ]))
    const w = mount(CompareDrawer, {
      props: { visible: true, runA: 1, runB: 2 },
      global: { plugins: [ElementPlus] },
    })
    await flushPromises()
    expect(evalApi.getRun).toHaveBeenCalledTimes(2)
    const rows = w.findAll('.items-table .el-table__row')
    expect(rows.length).toBe(2)
    // 列序:组 | 问题 | A-hit@k | A-MRR | A-关键词召回 | A-拒答 | B-...
    const legacyTds = rows[0]!.findAll('td')
    for (const td of legacyTds.slice(2, 5)) expect(td.text()).toBe('—')
    expect(rows[0]!.findAll('.score-low').length).toBe(0)
    const measuredTds = rows[1]!.findAll('td')
    for (const td of measuredTds.slice(2, 5)) expect(td.text()).toBe('0.00')
    expect(rows[1]!.findAll('.score-low').length).toBe(3)
  })
})
