import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import ElementPlus from 'element-plus'
import QuestionsTab from '@/pages/eval/QuestionsTab.vue'
import { evalApi, type EvalQuestion } from '@/api/eval'

vi.mock('@/api/eval', () => ({
  evalApi: {
    listQuestions: vi.fn(),
    createQuestion: vi.fn(),
    updateQuestion: vi.fn(),
    deleteQuestion: vi.fn(),
    myKbs: vi.fn(),
  },
}))

const kbs = [
  { kb_id: 3, kb_name: '测试库', question_count: 2 },
  { kb_id: 4, kb_name: '空库', question_count: 0 },
]

const questions: EvalQuestion[] = [
  {
    id: 11, kb_id: 3, question: '预算多少', expect_doc_ids: [1],
    expect_keywords: ['预算'], reference_answer: '三千万',
    created_at: '2026-09-21T10:00:00',
  },
]

describe('QuestionsTab', () => {
  beforeEach(() => {
    vi.mocked(evalApi.myKbs).mockReset()
    vi.mocked(evalApi.listQuestions).mockReset()
    vi.mocked(evalApi.createQuestion).mockReset()
    vi.mocked(evalApi.deleteQuestion).mockReset()
    vi.mocked(evalApi.myKbs).mockResolvedValue(kbs)
    vi.mocked(evalApi.listQuestions).mockResolvedValue(
      { total: questions.length, items: questions })
  })

  it('renders questions of selected kb', async () => {
    const w = mount(QuestionsTab, { global: { plugins: [ElementPlus] } })
    await flushPromises()
    expect(w.text()).toContain('预算多少')
    expect(w.text()).toContain('三千万')
    expect(w.text()).toContain('共 2 题') // question_count 提示
  })

  it('create dialog submits payload with kb', async () => {
    vi.mocked(evalApi.createQuestion).mockResolvedValue(questions[0]!)
    const w = mount(QuestionsTab, { global: { plugins: [ElementPlus] } })
    await flushPromises()
    await w.find('button.add-btn').trigger('click')
    await flushPromises()
    await w.find('textarea.q-input').setValue('新题目')
    await w.find('button.confirm-btn').trigger('click')
    await flushPromises()
    expect(evalApi.createQuestion).toHaveBeenCalledWith(
      expect.objectContaining({ kb_id: 3, question: '新题目' }))
  })

  it('delete asks confirm then calls api', async () => {
    vi.mocked(evalApi.deleteQuestion).mockResolvedValue(undefined)
    const w = mount(QuestionsTab, { global: { plugins: [ElementPlus] } })
    await flushPromises()
    await w.find('button.del-btn').trigger('click')
    await flushPromises()
    // ElMessageBox 是全局弹窗——组件内用 await ElMessageBox.confirm;
    // 测试里 stub:vi.mock('element-plus', ...) 过重,改为组件内
    // deleteConfirm ref + 自绘 el-dialog 确认(见实现),点确认按钮:
    await w.find('button.confirm-del-btn').trigger('click')
    await flushPromises()
    expect(evalApi.deleteQuestion).toHaveBeenCalledWith(11)
  })

  it('empty state guides to pick kb', async () => {
    vi.mocked(evalApi.listQuestions).mockResolvedValue(
      { total: 0, items: [] })
    const w = mount(QuestionsTab, { global: { plugins: [ElementPlus] } })
    await flushPromises()
    expect(w.text()).toContain('暂无题目')
  })
})
