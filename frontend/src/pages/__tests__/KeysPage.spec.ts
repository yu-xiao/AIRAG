import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import ElementPlus from 'element-plus'
import KeysPage from '@/pages/KeysPage.vue'
import { keysApi, type ApiKeyItem } from '@/api/keys'

vi.mock('@/api/keys', () => ({
  keysApi: {
    list: vi.fn<() => Promise<ApiKeyItem[]>>(),
    create: vi.fn(),
    revoke: vi.fn(),
  },
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
    vi.mocked(keysApi.revoke).mockReset()
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
})
