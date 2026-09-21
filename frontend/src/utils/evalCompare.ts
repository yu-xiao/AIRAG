import type { EvalItem } from '@/api/eval'
import { TREND_METRICS } from '@/utils/evalTrend'

export interface CompareRow {
  question: string
  only: 'both' | 'a' | 'b'
  a?: EvalItem
  b?: EvalItem
}

/** question 文本精确配对;未配对分「仅 A」「仅 B」尾随(spec D)。 */
export function joinItems(aItems: EvalItem[], bItems: EvalItem[]): CompareRow[] {
  const bByQ = new Map(bItems.map((i) => [i.question, i]))
  const rows: CompareRow[] = []
  const usedB = new Set<EvalItem>()
  for (const a of aItems) {
    const b = bByQ.get(a.question)
    if (b && !usedB.has(b)) {
      usedB.add(b)
      rows.push({ question: a.question, only: 'both', a, b })
    } else {
      rows.push({ question: a.question, only: 'a', a })
    }
  }
  for (const b of bItems) {
    if (!usedB.has(b)) rows.push({ question: b.question, only: 'b', b })
  }
  return rows
}

const LABELS: Record<string, string> = Object.fromEntries(
  [...TREND_METRICS.retrieval, ...TREND_METRICS.generation]
    .map((m) => [m.key, m.label]))

/** 汇总指标键 → EvalItem 得分字段(对比抽屉逐题列取数)。 */
export const ITEM_KEY: Record<string, keyof EvalItem> = {
  hit: 'hit_at_k',
  mrr: 'mrr',
  keyword_recall: 'keyword_recall',
  faithfulness_avg: 'faithfulness',
  relevancy_avg: 'relevancy',
  reference_avg: 'reference_score',
}

export interface SummaryDiffRow {
  key: string
  label: string
  a: number | null
  b: number | null
  delta: number | null
}

/** 两 summary 的共有数值键并排 + Δ(A−B);缺席侧 null、双侧缺席不入行。 */
export function computeSummaryDiff(
  a: Record<string, number | null> | null | undefined,
  b: Record<string, number | null> | null | undefined,
): SummaryDiffRow[] {
  const keys = [...new Set([...Object.keys(a ?? {}), ...Object.keys(b ?? {})])]
    .filter((k) => k !== 'item_count')
  return keys.map((k) => {
    const va = a?.[k] ?? null
    const vb = b?.[k] ?? null
    return {
      key: k, label: LABELS[k] ?? k, a: va, b: vb,
      delta: va != null && vb != null
        ? Math.round((va - vb) * 10000) / 10000 : null,
    }
  })
}
