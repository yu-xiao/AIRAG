import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import ElementPlus, { ElSelect } from 'element-plus'
import KeysPage from '@/pages/KeysPage.vue'
import { keysApi, type ApiKeyItem } from '@/api/keys'
import { useAuthStore } from '@/stores/auth'
import { usersApi } from '@/api/users'
import { kbApi } from '@/api/kb'

vi.mock('@/api/keys', () => ({
  keysApi: {
    list: vi.fn<() => Promise<ApiKeyItem[]>>(),
    create: vi.fn(),
    createAdmin: vi.fn(),
    adminUserKbs: vi.fn(),
    revoke: vi.fn(),
  },
}))
vi.mock('@/stores/auth', () => ({
  useAuthStore: vi.fn(() => ({ user: { id: 1, username: 'ed', role: 'editor' } })),
}))
vi.mock('@/api/users', () => ({
  usersApi: { search: vi.fn() },
}))
vi.mock('@/api/kb', () => ({
  kbApi: { list: vi.fn() },
}))

const items: ApiKeyItem[] = [
  {
    id: 1, name: 'Cursor 工作机', key_prefix: 'airag_AbCdEf', role: 'editor', is_active: true,
    kb_scope: null,
    expires_at: null, last_used_at: null, created_at: '2026-09-17T10:00:00',
  },
  {
    id: 2, name: '旧密钥', key_prefix: 'airag_XyZwVu', role: 'read_only', is_active: false,
    kb_scope: null,
    expires_at: null, last_used_at: null, created_at: '2026-09-16T10:00:00',
  },
]

const mountPage = () => mount(KeysPage, { global: { plugins: [ElementPlus] } })
// 精确匹配:页面同时存在「创建密钥」与对话框内「创建」,includes 会撞错按钮
const findBtn = (w: ReturnType<typeof mount>, text: string) =>
  w.findAll('button').find((b) => b.text().trim() === text)!

describe('KeysPage', () => {
  beforeEach(() => {
    vi.mocked(keysApi.list).mockResolvedValue(items)
    vi.mocked(keysApi.create).mockReset()
    vi.mocked(keysApi.createAdmin).mockReset()
    vi.mocked(keysApi.revoke).mockReset()
    vi.mocked(usersApi.search).mockReset()
    vi.mocked(kbApi.list).mockReset()
  })

  it('renders key list with status tags', async () => {
    const w = mountPage()
    await flushPromises()
    expect(w.text()).toContain('Cursor 工作机')
    expect(w.text()).toContain('airag_AbCdEf')
    expect(w.text()).toContain('已吊销')
  })

  it('create flow shows one-time plaintext key', async () => {
    vi.mocked(keysApi.create).mockResolvedValue({
      ...items[0]!, id: 9, key: 'airag_OneTimeSecret123',
    })
    const w = mountPage()
    await flushPromises()
    await findBtn(w, '创建密钥').trigger('click')
    await flushPromises()
    const input = w.find('input[placeholder="请输入密钥名称"]')
    await input.setValue('新密钥')
    await findBtn(w, '创建').trigger('click')
    await flushPromises()
    expect(keysApi.create).toHaveBeenCalledWith({
      name: '新密钥',
      role: 'read_only',
      expires_in_days: null,
      kb_scope: null,
    })
    await vi.waitFor(() => {
      expect(w.text()).toContain('airag_OneTimeSecret123')
    })
  })

  it('revoke asks confirm then calls api', async () => {
    vi.mocked(keysApi.revoke).mockResolvedValue(undefined)
    const w = mountPage()
    await flushPromises()
    // 第 1 行是活跃密钥 → 吊销按钮
    await findBtn(w, '吊销').trigger('click')
    await flushPromises()
    expect(keysApi.revoke).not.toHaveBeenCalled() // 等待确认框
  })

  it('admin flow: bind key to searched account via createAdmin', async () => {
    vi.mocked(useAuthStore).mockReturnValueOnce({
      user: { id: 1, username: 'boss', role: 'admin' },
    } as never)
    vi.mocked(usersApi.search).mockResolvedValue([{ id: 7, username: 'alice' }])
    vi.mocked(keysApi.createAdmin).mockResolvedValue({
      ...items[0]!, id: 11, key: 'airag_BoundOneTime9',
    })
    const w = mountPage()
    await flushPromises()
    await findBtn(w, '创建密钥').trigger('click')
    await flushPromises()
    expect(w.text()).toContain('绑定账号')
    // 直接调组件的 remote-method(绕开 jsdom 输入事件)
    const sel = w.findComponent(ElSelect)
    await (sel.props('remoteMethod') as (q: string) => void)('ali')
    await flushPromises()
    expect(usersApi.search).toHaveBeenCalledWith('ali', 0, 20)
    await sel.vm.$emit('update:modelValue', 7)
    await flushPromises()
    await w.find('input[placeholder="请输入密钥名称"]').setValue('代管key')
    await findBtn(w, '创建').trigger('click')
    await flushPromises()
    expect(keysApi.createAdmin).toHaveBeenCalledWith({
      user_id: 7, name: '代管key', role: 'read_only', expires_in_days: null,
      kb_scope: null,
    })
    expect(keysApi.create).not.toHaveBeenCalled()
    await vi.waitFor(() => {
      expect(w.text()).toContain('airag_BoundOneTime9')
      expect(w.text()).toContain('alice') // 已绑定账号提示
    })
  })

  it('non-admin create dialog has no account binding', async () => {
    const w = mountPage() // 默认 mock 为 editor
    await flushPromises()
    await findBtn(w, '创建密钥').trigger('click')
    await flushPromises()
    expect(w.text()).not.toContain('绑定账号')
    // M12:非 admin 表单仍有范围选择 el-select,判据改为「无绑定账号下拉」
    expect(
      w.findAllComponents(ElSelect).filter(
        (s) => s.props('placeholder') === '默认绑定当前账号',
      ),
    ).toHaveLength(0)
  })
})

describe('KeysPage kb scope picker', () => {
  const scopedItem: ApiKeyItem = {
    id: 3, name: '范围密钥', key_prefix: 'airag_Scoped', role: 'read_only',
    is_active: true, kb_scope: [1, 2],
    expires_at: null, last_used_at: null, created_at: '2026-09-19T10:00:00',
  }

  it('renders scope badge (全部 / N 库)', async () => {
    vi.mocked(keysApi.list).mockResolvedValue([scopedItem])
    const w = mountPage()
    await flushPromises()
    // VTU text() 是原生 textContent:表头「范围」与徽标「2 库」在 DOM 中不相邻,
    // 组不出「范围 2 库」连续串,改为直接断言徽标 tag 文本
    const badges = w.findAll('.el-tag').map((t) => t.text())
    expect(badges).toContain('2 库')
    expect(badges).not.toContain('全部')
  })

  it('empty selection submits kb_scope null; options from own kbs', async () => {
    vi.mocked(keysApi.list).mockResolvedValue([])
    vi.mocked(kbApi.list).mockResolvedValue([
      { id: 1, name: '库一' } as never,
      { id: 2, name: '库二' } as never,
    ])
    vi.mocked(keysApi.create).mockResolvedValue({
      ...scopedItem, id: 20, key: 'airag_ScopedOneTime',
    })
    const w = mountPage()
    await flushPromises()
    await findBtn(w, '创建密钥').trigger('click')
    await flushPromises()
    expect(kbApi.list).toHaveBeenCalled()  // 打开即载入自己的库
    await w.find('input[placeholder="请输入密钥名称"]').setValue('范围key')
    await findBtn(w, '创建').trigger('click')
    await flushPromises()
    expect(keysApi.create).toHaveBeenCalledWith({
      name: '范围key', role: 'read_only', expires_in_days: null,
      kb_scope: null,
    })
  })

  it('selecting kbs submits ids', async () => {
    vi.mocked(keysApi.list).mockResolvedValue([])
    vi.mocked(kbApi.list).mockResolvedValue([
      { id: 1, name: '库一' } as never,
      { id: 2, name: '库二' } as never,
    ])
    vi.mocked(keysApi.create).mockResolvedValue({
      ...scopedItem, id: 21, key: 'airag_ScopedTwoTime',
    })
    const w = mountPage()
    await flushPromises()
    await findBtn(w, '创建密钥').trigger('click')
    await flushPromises()
    const sel = w.findComponent(ElSelect)  // 非 admin:表单内唯一 select
    await sel.vm.$emit('update:modelValue', [1, 2])
    await flushPromises()
    await w.find('input[placeholder="请输入密钥名称"]').setValue('选库key')
    await findBtn(w, '创建').trigger('click')
    await flushPromises()
    expect(keysApi.create).toHaveBeenCalledWith({
      name: '选库key', role: 'read_only', expires_in_days: null,
      kb_scope: [1, 2],
    })
  })

  it('admin mode loads target user kbs after binding account', async () => {
    vi.mocked(useAuthStore).mockReturnValueOnce({
      user: { id: 1, username: 'boss', role: 'admin' },
    } as never)
    vi.mocked(keysApi.list).mockResolvedValue([])
    vi.mocked(usersApi.search).mockResolvedValue([{ id: 7, username: 'alice' }])
    vi.mocked(keysApi.adminUserKbs).mockResolvedValue([
      { id: 5, name: '目标用户的库' },
    ])
    vi.mocked(keysApi.createAdmin).mockResolvedValue({
      ...scopedItem, id: 22, key: 'airag_AdminScoped9',
    })
    const w = mountPage()
    await flushPromises()
    await findBtn(w, '创建密钥').trigger('click')
    await flushPromises()
    const sels = w.findAllComponents(ElSelect)
    const bindSel = sels.find((s) => s.props('placeholder') === '默认绑定当前账号')!
    await (bindSel.props('remoteMethod') as (q: string) => void)('ali')
    await flushPromises()
    await bindSel.vm.$emit('update:modelValue', 7)
    await flushPromises()
    expect(keysApi.adminUserKbs).toHaveBeenCalledWith(7)
    await w.find('input[placeholder="请输入密钥名称"]').setValue('代发范围key')
    const scopeSel = sels.find((s) =>
      s.props('placeholder') === '不限(全部授权库)')!
    await scopeSel.vm.$emit('update:modelValue', [5])
    await flushPromises()
    await findBtn(w, '创建').trigger('click')
    await flushPromises()
    expect(keysApi.createAdmin).toHaveBeenCalledWith({
      user_id: 7, name: '代发范围key', role: 'read_only',
      expires_in_days: null, kb_scope: [5],
    })
  })
})
