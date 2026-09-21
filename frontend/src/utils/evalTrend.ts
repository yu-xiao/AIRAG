import type { EvalRun } from '@/api/eval'

export interface TrendData {
  times: string[]
  series: { name: string; data: (number | null)[] }[]
}

/** 只取 completed 且有 summary 的 run,时间升序成线;缺席值 null。 */
export function buildTrendSeries(runs: EvalRun[], metrics: string[]): TrendData {
  const usable = runs
    .filter((r) => r.status === 'completed' && r.summary)
    .slice()
    .sort((a, b) => a.created_at.localeCompare(b.created_at))
  const times = usable.map((r) => r.created_at)
  const series = metrics.map((m) => ({
    name: m,
    data: usable.map((r) => {
      const v = r.summary?.[m]
      return v == null ? null : Number(v)
    }),
  }))
  return { times, series }
}

export const TREND_METRICS: Record<'retrieval' | 'generation',
  { key: string; label: string }[]> = {
  retrieval: [
    { key: 'hit', label: '命中率' },
    { key: 'mrr', label: 'MRR' },
    { key: 'keyword_recall', label: '关键词召回' },
  ],
  generation: [
    { key: 'faithfulness_avg', label: '忠实度' },
    { key: 'relevancy_avg', label: '相关性' },
    { key: 'reference_avg', label: '参考一致' },
  ],
}
