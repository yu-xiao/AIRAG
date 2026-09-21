import { describe, expect, it } from 'vitest'
import { computeSummaryDiff, joinItems } from '@/utils/evalCompare'
import type { EvalItem } from '@/api/eval'

const item = (q: string, hit: number | null): EvalItem => ({
  id: 0, question: q, expect_doc_ids: [], expect_keywords: [],
  answer: null, refused: false, hit_at_k: hit, mrr: hit,
  keyword_recall: hit, faithfulness: null, relevancy: null,
  reference_score: null,
})

describe('joinItems', () => {
  it('pairs by exact question, then only-a, then only-b', () => {
    const rows = joinItems(
      [item('q1', 1), item('q2', 1)],
      [item('q1', 0), item('q3', 0)],
    )
    expect(rows.map((r) => [r.question, r.only])).toEqual([
      ['q1', 'both'], ['q2', 'a'], ['q3', 'b'],
    ])
  })
})

describe('computeSummaryDiff', () => {
  it('computes delta with missing as null', () => {
    const rows = computeSummaryDiff(
      { hit: 0.8, mrr: 0.6 }, { hit: 0.5, mrr: null })
    const hit = rows.find((r) => r.key === 'hit')!
    expect(hit.a).toBe(0.8)
    expect(hit.delta).toBeCloseTo(0.3)
    expect(rows.find((r) => r.key === 'mrr')!.delta).toBeNull()
  })
})
