import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import ElementPlus from 'element-plus'
import KbPage from '@/pages/KbPage.vue'
import { kbApi } from '@/api/kb'

const push = vi.fn()

vi.mock('vue-router', () => ({
  useRoute: () => ({ query: { create: '1' } }),  // 挂载即开创建对话框
  useRouter: () => ({ push }),
}))
vi.mock('@/stores/auth', () => ({
  useAuthStore: () => ({ user: { id: 1, username: 'ed', role: 'editor' } }),
}))
vi.mock('@/api/kb', () => ({
  kbApi: { list: vi.fn(), create: vi.fn() },
}))

const nameInput = 'input[placeholder="请输入知识库名称"]'
const createBtn = (w: ReturnType<typeof mount>) =>
  w.findAll('button').find((b) => b.text() === '创建')!
const mountPage = () => mount(KbPage, { global: { plugins: [ElementPlus] } })

describe('KbPage create dialog', () => {
  beforeEach(() => {
    vi.mocked(kbApi.list).mockResolvedValue([])
    push.mockClear()
  })

  it('409 shows inline name error and keeps dialog open with input', async () => {
    vi.mocked(kbApi.create).mockRejectedValue({
      response: { status: 409, data: { detail: 'knowledge base name already exists' } },
    })
    const w = mountPage()
    await flushPromises()
    await w.find(nameInput).setValue('重名库')
    await createBtn(w).trigger('click')
    await flushPromises()
    // EP form-item 的 error 状态经 100ms refDebounced 防抖后才渲染,轮询等待
    await vi.waitFor(() => {
      expect(w.find('.el-form-item__error').exists()).toBe(true)
    })
    const err = w.find('.el-form-item__error')
    expect(err.text()).toContain('已存在')
    expect((w.find(nameInput).element as HTMLInputElement).value).toBe('重名库')
  })

  it('non-409 falls back to global message without inline error', async () => {
    vi.mocked(kbApi.create).mockRejectedValue({
      response: { status: 500, data: { detail: 'boom' } },
    })
    const w = mountPage()
    await flushPromises()
    await w.find(nameInput).setValue('普通库')
    await createBtn(w).trigger('click')
    await flushPromises()
    expect(w.find('.el-form-item__error').exists()).toBe(false)
  })
})
