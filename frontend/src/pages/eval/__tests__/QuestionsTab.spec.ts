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
    exportQuestions: vi.fn(),
    bulkImport: vi.fn(),
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
    vi.mocked(evalApi.exportQuestions).mockReset()
    vi.mocked(evalApi.bulkImport).mockReset()
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

  // ---- M19 T6:导入 / 导出 ----

  it('import flow:file 读入→confirm→bulkImport→成功消息并刷新', async () => {
    vi.mocked(evalApi.bulkImport)
      .mockResolvedValue({ created: 3, errors: [] })
    const { ElMessage, ElMessageBox } = await import('element-plus')
    const confirmSpy = vi.spyOn(ElMessageBox, 'confirm')
      .mockResolvedValue(undefined as never)
    const successSpy = vi.spyOn(ElMessage, 'success')
    const w = mount(QuestionsTab, { global: { plugins: [ElementPlus] } })
    await flushPromises()
    const q1 = { question: '导入题1', expect_keywords: ['a'] }
    const q2 = { question: '导入题2' }
    const q3 = { question: '导入题3', reference_answer: '参考' }
    // 导出格式({questions:[...]})夹带一条非 dict 垃圾:过滤后才发(T5:非 dict 整体 422)
    const file = new File(
      [JSON.stringify({ count: 4, questions: [q1, 'junk', q2, q3] })],
      'eval-questions-kb3.json', { type: 'application/json' })
    const input = w.find('input.import-file')
    Object.defineProperty(input.element, 'files', { value: [file] })
    await input.trigger('change')
    await flushPromises()
    expect(confirmSpy).toHaveBeenCalledWith('将导入 3 题', '导入题集')
    expect(evalApi.bulkImport).toHaveBeenCalledWith(3, [q1, q2, q3])
    expect(successSpy).toHaveBeenCalledWith('3 题导入成功')
    expect(evalApi.listQuestions).toHaveBeenCalledTimes(2) // 导入后刷新
    confirmSpy.mockRestore()
    successSpy.mockRestore()
  })

  it('export disabled without questions;enabled 时点击下载 blob 附件', async () => {
    const { ElMessage } = await import('element-plus')
    const errSpy = vi.spyOn(ElMessage, 'error')
    // 空题集:导出禁用
    vi.mocked(evalApi.listQuestions).mockResolvedValue({ total: 0, items: [] })
    const empty = mount(QuestionsTab, { global: { plugins: [ElementPlus] } })
    await flushPromises()
    expect(empty.find('button.export-btn').attributes('disabled'))
      .toBeDefined()
    // 有题:可点 → exportQuestions + a[download] + revoke
    vi.mocked(evalApi.listQuestions)
      .mockResolvedValue({ total: questions.length, items: questions })
    const w = mount(QuestionsTab, { global: { plugins: [ElementPlus] } })
    await flushPromises()
    expect(w.find('button.export-btn').attributes('disabled')).toBeUndefined()
    vi.mocked(evalApi.exportQuestions)
      .mockResolvedValue(new Blob(['{}'], { type: 'application/json' }))
    // jsdom 未实现 objectURL API:直接挂桩(测毕还原)
    const urlStub = URL as unknown as Record<string, unknown>
    const createObjectURL = vi.fn(() => 'blob:mock')
    const revokeObjectURL = vi.fn()
    urlStub.createObjectURL = createObjectURL
    urlStub.revokeObjectURL = revokeObjectURL
    const clickSpy = vi.spyOn(HTMLElement.prototype, 'click')
      .mockImplementation(() => {}) // 拦截 jsdom 未实现的导航
    await w.find('button.export-btn').trigger('click')
    await flushPromises()
    // 调用方(this)即下载锚点,经 mock.contexts 取回(lib 无 .at,下标访问)
    const ctxs = clickSpy.mock.contexts
    const anchor = ctxs[ctxs.length - 1] as HTMLAnchorElement | undefined
    expect(evalApi.exportQuestions).toHaveBeenCalledWith(3)
    expect(createObjectURL).toHaveBeenCalledTimes(1)
    expect(anchor?.download).toBe('eval-questions-kb3.json')
    expect(anchor?.href).toBe('blob:mock')
    expect(revokeObjectURL).toHaveBeenCalledWith('blob:mock')
    expect(errSpy).not.toHaveBeenCalled()
    urlStub.createObjectURL = undefined
    urlStub.revokeObjectURL = undefined
    clickSpy.mockRestore()
    errSpy.mockRestore()
  })
})
