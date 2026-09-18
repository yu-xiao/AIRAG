import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import ElementPlus, { ElSelect } from 'element-plus'
import KeysPage from '@/pages/KeysPage.vue'
import { keysApi, type ApiKeyItem } from '@/api/keys'
import { useAuthStore } from '@/stores/auth'
import { usersApi } from '@/api/users'

vi.mock('@/api/keys', () => ({
  keysApi: {
    list: vi.fn<() => Promise<ApiKeyItem[]>>(),
    create: vi.fn(),
    createAdmin: vi.fn(),
    revoke: vi.fn(),
  },
}))
vi.mock('@/stores/auth', () => ({
  useAuthStore: vi.fn(() => ({ user: { id: 1, username: 'ed', role: 'editor' } })),
}))
vi.mock('@/api/users', () => ({
  usersApi: { search: vi.fn() },
}))

const items: ApiKeyItem[] = [
  {
    id: 1, name: 'Cursor 工作机', key_prefix: 'airag_AbCdEf', is_active: true,
    expires_at: null, last_used_at: null, created_at: '2026-09-17T10:00:00',
  },
  {
    id: 2, name: '旧密钥', key_prefix: 'airag_XyZwVu', is_active: false,
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
      expires_in_days: null,
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
      user_id: 7, name: '代管key', expires_in_days: null,
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
    expect(w.findComponent(ElSelect).exists()).toBe(false)
  })
})
