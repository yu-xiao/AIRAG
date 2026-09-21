import http from './http'

/** 镜像后端 EvalRunOut(app/schemas/eval.py);summary 键与 eval_store.summarize 口径一致 */
export interface EvalRun {
  id: number
  kb_id: number
  kb_name: string | null
  mode: 'retrieval' | 'generation'
  item_count: number
  summary: Record<string, number | null> | null
  status: 'running' | 'completed' | 'failed'
  created_by: string | null
  done_count: number
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
  error: string | null
}

export interface EvalRunQuery {
  kb_id?: number
  mode?: 'retrieval' | 'generation'
  page?: number
  page_size?: number
}

/** 镜像后端 EvalQuestionOut */
export interface EvalQuestion {
  id: number
  kb_id: number
  question: string
  expect_doc_ids: number[] | null
  expect_keywords: string[] | null
  reference_answer: string | null
  created_at: string
}

/** 创建/更新题目入参 */
export interface QuestionInput {
  question: string
  expect_doc_ids?: number[]
  expect_keywords?: string[]
  reference_answer?: string | null
}

/** 镜像后端 MyKbOut:我有权管理题集的知识库及题目数 */
export interface MyKb {
  kb_id: number
  kb_name: string
  question_count: number
}

/** 触发评估运行入参 */
export interface TriggerInput {
  kb_id: number
  mode: 'retrieval' | 'generation'
  rerank?: boolean
  top_k?: number
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

  async listQuestions(params: {
    kb_id: number
    page?: number
    page_size?: number
  }): Promise<{ total: number; items: EvalQuestion[] }> {
    const { data } = await http.get('/eval/questions', { params })
    return data
  },

  async createQuestion(
    payload: QuestionInput & { kb_id: number },
  ): Promise<EvalQuestion> {
    const { data } = await http.post('/eval/questions', payload)
    return data
  },

  async updateQuestion(id: number, payload: QuestionInput): Promise<EvalQuestion> {
    const { data } = await http.put(`/eval/questions/${id}`, payload)
    return data
  },

  async deleteQuestion(id: number): Promise<void> {
    await http.delete(`/eval/questions/${id}`)
  },

  async myKbs(): Promise<MyKb[]> {
    const { data } = await http.get('/eval/my-kbs')
    return data
  },

  async triggerRun(payload: TriggerInput): Promise<{ run_id: number }> {
    const { data } = await http.post('/eval/runs', payload)
    return data
  },
}
