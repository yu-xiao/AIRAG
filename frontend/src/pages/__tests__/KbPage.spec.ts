import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import ElementPlus, { ElSelect } from 'element-plus'
import KbPage from '@/pages/KbPage.vue'
import { kbApi, type KbIn, type KbItem, type KbMember } from '@/api/kb'
import { usersApi } from '@/api/users'

const push = vi.fn<(to: unknown) => Promise<void>>()

vi.mock('vue-router', () => ({
  useRoute: () => ({ query: { create: '1' } }),  // 挂载即开创建对话框
  useRouter: () => ({ push }),
}))
vi.mock('@/stores/auth', () => ({
  useAuthStore: () => ({ user: { id: 1, username: 'ed', role: 'editor' } }),
}))
vi.mock('@/api/kb', () => ({
  kbApi: {
    list: vi.fn<() => Promise<KbItem[]>>(),
    create: vi.fn<(payload: KbIn) => Promise<KbItem>>(),
    members: vi.fn(),
    grant: vi.fn(),
  },
}))
vi.mock('@/api/users', () => ({
  usersApi: { search: vi.fn() },
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

describe('KbPage member grant remote dropdown', () => {
  const owned: KbItem = {
    id: 5, name: '成员库', description: null, owner_id: 1,
    embed_provider: 'fake', embed_model: 'x', my_perm: 'owner',
    doc_count: 0, created_at: '2026-09-18T10:00:00',
  }
  const mountPage2 = () => mount(KbPage, { global: { plugins: [ElementPlus] } })

  it('searches users remotely and grants by username', async () => {
    vi.mocked(kbApi.list).mockResolvedValue([owned])
    vi.mocked(kbApi.members).mockResolvedValue([])
    vi.mocked(kbApi.grant).mockResolvedValue({
      user_id: 2, username: 'alice', perm: 'viewer',
    } as KbMember)
    vi.mocked(usersApi.search).mockResolvedValue([{ id: 2, username: 'alice' }])
    const w = mountPage2()
    await flushPromises()
    await w.findAll('button').find((b) => b.text().trim() === '成员')!.trigger('click')
    await flushPromises()
    const userSel = w
      .findAllComponents(ElSelect)
      .find((s) => s.props('placeholder') === '输入用户名搜索')!
    await (userSel.props('remoteMethod') as (q: string) => void)('ali')
    await flushPromises()
    expect(usersApi.search).toHaveBeenCalledWith('ali', 0, 20)
    await userSel.vm.$emit('update:modelValue', 'alice')
    await flushPromises()
    await w.findAll('button').find((b) => b.text().trim() === '添加')!.trigger('click')
    await flushPromises()
    expect(kbApi.grant).toHaveBeenCalledWith(5, { username: 'alice', perm: 'viewer' })
  })
})
