import http from './http'

/** 镜像后端 EvalRunOut(app/schemas/eval.py);summary 键与 eval_store.summarize 口径一致 */
export interface EvalRun {
  id: number
  kb_id: number
  kb_name: string | null
  mode: 'retrieval' | 'generation'
  item_count: number
  summary: Record<string, number | null>
  created_at: string
}

/** 镜像后端 EvalItemOut */
export interface EvalItem {
  id: number
  question: string
  expect_doc_ids: number[] | null
  expect_keywords: string[] | null
  answer: string | null
  refused: boolean | null
  hit_at_k: number | null
  mrr: number | null
  keyword_recall: number | null
  faithfulness: number | null
  relevancy: number | null
  reference_score: number | null
}

/** 镜像后端 EvalRunDetailOut */
export interface EvalRunDetail extends EvalRun {
  items: EvalItem[]
  items_truncated: boolean
}

export interface EvalRunQuery {
  kb_id?: number
  mode?: 'retrieval' | 'generation'
  page?: number
  page_size?: number
}

export const evalApi = {
  async listRuns(
    params: EvalRunQuery,
  ): Promise<{ total: number; items: EvalRun[] }> {
    const { data } = await http.get('/eval/runs', { params })
    return data
  },

  async getRun(id: number): Promise<EvalRunDetail> {
    const { data } = await http.get(`/eval/runs/${id}`)
    return data
  },
}
