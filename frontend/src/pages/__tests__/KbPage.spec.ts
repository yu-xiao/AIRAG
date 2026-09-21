import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import ElementPlus, { ElMessageBox, ElSelect } from 'element-plus'
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
    remove: vi.fn(),
    rename: vi.fn(),
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

describe('KbPage delete kb', () => {
  const owned2: KbItem = {
    id: 9, name: '待删库', description: null, owner_id: 1,
    embed_provider: 'fake', embed_model: 'x', my_perm: 'owner',
    doc_count: 2, created_at: '2026-09-19T10:00:00',
  }
  const notOwned: KbItem = { ...owned2, id: 10, name: '成员库', my_perm: 'editor' }

  // vitest 未开 clearMocks,前面 describe 的调用会累积,先清计数再断言次数
  beforeEach(() => {
    vi.mocked(kbApi.list).mockClear()
    vi.mocked(kbApi.remove).mockReset()
  })

  it('owner card shows delete button; member card does not', async () => {
    vi.mocked(kbApi.list).mockResolvedValue([owned2, notOwned])
    const w = mount(KbPage, { global: { plugins: [ElementPlus] } })
    await flushPromises()
    const delBtns = w.findAll('button').filter((b) => b.text().trim() === '删除')
    expect(delBtns.length).toBe(1)  // 只有 owner 卡片有
  })

  it('remove() confirms then deletes and reloads; 409 shows detail', async () => {
    const confirmSpy = vi.spyOn(ElMessageBox, 'confirm')
      .mockResolvedValue(undefined as never) // 组件只 await 不取值;绕过 MessageBoxData 类型
    vi.mocked(kbApi.list).mockResolvedValue([owned2])
    vi.mocked(kbApi.remove).mockResolvedValue(undefined)
    const w = mount(KbPage, { global: { plugins: [ElementPlus] } })
    await flushPromises()
    const vm = w.vm as unknown as {
      remove: (row: KbItem) => Promise<void>
    }
    await vm.remove(owned2)
    await flushPromises()
    expect(confirmSpy).toHaveBeenCalled()
    expect(kbApi.remove).toHaveBeenCalledWith(9)
    expect(kbApi.list).toHaveBeenCalledTimes(2)  // 挂载 + 删除后 reload
    // 409 路径
    vi.mocked(kbApi.remove).mockRejectedValueOnce({
      response: { status: 409,
        data: { detail: 'knowledge base has documents being processed' } },
    })
    await vm.remove(owned2)
    await flushPromises()
    expect(kbApi.remove).toHaveBeenCalledTimes(2)
    confirmSpy.mockRestore()
  })
})

describe('KbPage rename dialog', () => {
  const owned4: KbItem = {
    id: 12, name: '旧名库', description: '旧描述', owner_id: 1,
    embed_provider: 'fake', embed_model: 'x', my_perm: 'owner',
    doc_count: 0, created_at: '2026-09-20T10:00:00',
  }

  it('owner card opens edit dialog prefilled; submit calls rename', async () => {
    vi.mocked(kbApi.list).mockResolvedValue([owned4])
    vi.mocked(kbApi.rename).mockResolvedValue({ ...owned4, name: '新名库' })
    const w = mount(KbPage, { global: { plugins: [ElementPlus] } })
    await flushPromises()
    await w.findAll('button').find((b) =>
      b.text().trim() === '重命名')!.trigger('click')
    await flushPromises()
    expect(w.text()).toContain('重命名知识库')
    // tsconfig lib < es2022 无 Array.at,用 slice(-1)[0] 等价取最后一个
    const nameInput = w.findAll('input[placeholder="请输入知识库名称"]')
      .slice(-1)[0] as ReturnType<typeof w.find>
    expect((nameInput.element as HTMLInputElement).value).toBe('旧名库')
    await nameInput.setValue('新名库')
    await w.findAll('button').find((b) =>
      b.text() === '保存')!.trigger('click')
    await flushPromises()
    expect(kbApi.rename).toHaveBeenCalledWith(12, {
      name: '新名库', description: '旧描述',
    })
  })

  it('409 shows inline name error and keeps dialog open', async () => {
    vi.mocked(kbApi.list).mockResolvedValue([owned4])
    vi.mocked(kbApi.rename).mockRejectedValue({
      response: { status: 409, data: { detail: 'knowledge base name already exists' } },
    })
    const w = mount(KbPage, { global: { plugins: [ElementPlus] } })
    await flushPromises()
    await w.findAll('button').find((b) =>
      b.text().trim() === '重命名')!.trigger('click')
    await flushPromises()
    const nameInput = w.findAll('input[placeholder="请输入知识库名称"]')
      .slice(-1)[0] as ReturnType<typeof w.find>
    await nameInput.setValue('重名库')
    await w.findAll('button').find((b) =>
      b.text() === '保存')!.trigger('click')
    await flushPromises()
    await vi.waitFor(() => {
      expect(w.find('.el-form-item__error').exists()).toBe(true)
    })
    expect(w.find('.el-form-item__error').text()).toContain('已存在')
  })
})

describe('KbPage clear description (M14)', () => {
  const ownerKb: KbItem = {
    id: 5, name: '可清库', description: '旧描述', owner_id: 1,
    embed_provider: 'zhipu', embed_model: 'embedding-3',
    created_at: '2026-09-21T10:00:00', my_perm: 'owner', doc_count: 0,
  }

  it('rename submits empty string as explicit clear', async () => {
    vi.mocked(kbApi.list).mockResolvedValue([ownerKb])
    vi.mocked(kbApi.rename).mockResolvedValue({ ...ownerKb, description: null })
    const w = mountPage()
    await flushPromises()
    await w.findAll('button').find((b) => b.text().trim() === '重命名')!.trigger('click')
    await flushPromises()
    // 挂载即开的创建对话框(route query create=1)其 textarea DOM 在取消后
    // 仍保留(el-dialog 默认不销毁),且模板序在前——取最后一个才是重命名
    // 对话框的描述框(KbPage.vue:414)
    const areas = w.findAll('textarea')
    await areas[areas.length - 1]!.setValue('')
    await w.findAll('button').find((b) => b.text().trim() === '保存')!.trigger('click')
    await flushPromises()
    expect(kbApi.rename).toHaveBeenCalledWith(5, {
      name: '可清库',
      description: '',  // M14:空串直发(旧实现发 null → 清空无效)
    })
  })
})
