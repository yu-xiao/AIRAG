import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import ElementPlus, { type UploadRawFile } from 'element-plus'
import DocsPage from '@/pages/DocsPage.vue'
import DocsPageSource from '@/pages/DocsPage.vue?raw'
import { documentsApi } from '@/api/documents'
import { kbApi, type KbItem } from '@/api/kb'

const push = vi.fn<(to: unknown) => Promise<void>>()
const replace = vi.fn<(to: unknown) => Promise<void>>()

vi.mock('vue-router', () => ({
  useRoute: () => ({ params: { id: '1' } }),
  useRouter: () => ({ push, replace }),
}))
vi.mock('@/api/documents', () => ({
  // 挂载路径只调 list;upload/chunks/reprocess/remove 不在本用例内
  documentsApi: { list: vi.fn() },
}))
vi.mock('@/api/kb', () => ({
  kbApi: { detail: vi.fn() },
}))

const ownedKb: KbItem = {
  id: 1, name: '上传库', description: null, owner_id: 1,
  embed_provider: 'fake', embed_model: 'x', created_at: '2026-09-30T10:00:00',
  my_perm: 'owner', doc_count: 0,
}

describe('DocsPage beforeUpload allowlist (M24: 18 types)', () => {
  beforeEach(() => {
    vi.mocked(kbApi.detail).mockResolvedValue(ownedKb)
    vi.mocked(documentsApi.list).mockResolvedValue([])
  })

  it('accepts new types .txt/.pptx and still rejects .exe', async () => {
    const w = mount(DocsPage, { global: { plugins: [ElementPlus] } })
    await flushPromises()
    // canEdit(owner)为真才渲染 el-upload;经组件 props 取页面绑定的钩子
    const upload = w.findComponent({ name: 'ElUpload' })
    expect(upload.exists()).toBe(true)
    const hook = upload.props('beforeUpload') as unknown as (
      raw: UploadRawFile,
    ) => boolean | undefined
    const f = (name: string) => ({ name, size: 8 }) as unknown as UploadRawFile
    expect(hook(f('a.txt'))).toBe(true)   // 旧白名单缺 .txt(M24 终审回归锁)
    expect(hook(f('b.pptx'))).toBe(true)
    expect(hook(f('c.exe'))).toBe(false)  // 越界类型仍拦截
  })

  it('拒绝文案与 18 项门清单一致(含 .htm/.jpeg 别名)', () => {
    expect(DocsPageSource).toContain('.html/.htm')
    expect(DocsPageSource).toContain('.jpeg')
  })
})
