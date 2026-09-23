import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import ElementPlus, { ElSwitch } from 'element-plus'
import WebhooksPage from '@/pages/WebhooksPage.vue'
import { adminApi, type WebhookDeliveryResponse, type WebhookEndpoint } from '@/api/admin'

vi.mock('@/api/admin', () => ({
  adminApi: {
    listWebhooks: vi.fn(),
    createWebhook: vi.fn(),
    updateWebhook: vi.fn(),
    deleteWebhook: vi.fn(),
    testWebhook: vi.fn(),
    listWebhookDeliveries: vi.fn(),
  },
}))

const eps: WebhookEndpoint[] = [
  {
    id: 1, name: '面板端点', url: 'https://panel.example.com/hook',
    events: ['document.done', 'eval.completed'], enabled: true,
    description: null, secret_masked: 'wh_****ab12',
    created_at: '2026-09-22T10:00:00',
    // 反证字段:后端 list 永不回传明文,页面若误渲染 row.secret 此处即露馅
    secret: 'wh_plain_SECRET_XYZ',
  } as WebhookEndpoint,
  {
    id: 2, name: '备份端点', url: 'https://backup.example.com/hook',
    events: null, enabled: false,
    description: null, secret_masked: 'wh_****cd34',
    created_at: '2026-09-22T11:00:00',
  },
]

const deliveries: WebhookDeliveryResponse = {
  total: 2,
  items: [
    {
      id: 11, endpoint_id: 1, endpoint_name: '面板端点', event_type: 'document.done',
      status: 'dead', attempts: 6, response_status: 500,
      last_error: 'retryable 500', payload: null, next_attempt_at: null,
      created_at: '2026-09-22T12:00:00',
    },
    {
      id: 12, endpoint_id: 2, endpoint_name: '备份端点', event_type: 'chat.refused',
      status: 'retrying', attempts: 1, response_status: null,
      last_error: 'connect timeout', payload: null,
      next_attempt_at: '2026-09-22T12:05:00', created_at: '2026-09-22T12:01:00',
    },
  ],
}

const mountPage = () => mount(WebhooksPage, { global: { plugins: [ElementPlus] } })
// 精确匹配:工具行「新建端点」/操作列「测试」等与对话框按钮须以全文相等区分
const findBtn = (w: ReturnType<typeof mount>, text: string) =>
  w.findAll('button').find((b) => b.text().trim() === text)!

describe('WebhooksPage', () => {
  beforeEach(() => {
    vi.mocked(adminApi.listWebhooks).mockReset()
    vi.mocked(adminApi.createWebhook).mockReset()
    vi.mocked(adminApi.updateWebhook).mockReset()
    vi.mocked(adminApi.deleteWebhook).mockReset()
    vi.mocked(adminApi.testWebhook).mockReset()
    vi.mocked(adminApi.listWebhookDeliveries).mockReset()
    vi.mocked(adminApi.listWebhooks).mockResolvedValue(eps)
  })

  it('renders endpoints with masked secret and all-events tag', async () => {
    const w = mountPage()
    await flushPromises()
    expect(w.text()).toContain('面板端点')
    expect(w.text()).toContain('https://panel.example.com/hook')
    expect(w.text()).toContain('wh_****ab12') // masked 只露尾 4
    expect(w.text()).not.toContain('wh_plain_SECRET_XYZ') // 明文绝不出现
    // events=null 行显示单个灰 tag「全部」;非空行逐事件出 tag
    const tags = w.findAll('.ep-table .el-tag').map((t) => t.text())
    expect(tags).toContain('全部')
    expect(tags).toContain('文档解析完成')
  })

  it('create dialog submits and shows one-time secret', async () => {
    vi.mocked(adminApi.createWebhook).mockResolvedValue({
      ...eps[1]!, id: 9, secret: 'wh_plainsecret123',
    })
    const w = mountPage()
    await flushPromises()
    await findBtn(w, '新建端点').trigger('click')
    await flushPromises()
    await w.find('input[placeholder="请输入端点名称"]').setValue('新端点')
    await w
      .find('input[placeholder="https://example.com/webhook"]')
      .setValue('https://new.example.com/hook')
    await findBtn(w, '保存').trigger('click')
    await flushPromises()
    expect(adminApi.createWebhook).toHaveBeenCalledWith({
      name: '新端点',
      url: 'https://new.example.com/hook',
      events: [], // 空数组 = 订阅全部
    })
    await vi.waitFor(() => {
      // 一次性弹窗:明文 + 「仅此一次」警示文案
      expect(w.text()).toContain('wh_plainsecret123')
      expect(w.text()).toContain('仅此一次')
    })
  })

  it('toggle switch calls updateWebhook', async () => {
    vi.mocked(adminApi.updateWebhook).mockResolvedValue({ ...eps[0]!, enabled: false })
    const w = mountPage()
    await flushPromises()
    // 首行 enabled=true,表格内直切(:model-value 单向绑定)→ 以 {enabled:false} 落库
    const sw = w.findAllComponents(ElSwitch)[0]!
    await sw.vm.$emit('change', false)
    await flushPromises()
    expect(adminApi.updateWebhook).toHaveBeenCalledWith(1, { enabled: false })
  })

  it('test button calls testWebhook and reports', async () => {
    vi.mocked(adminApi.testWebhook).mockResolvedValue({
      status: 'succeeded', response_status: 200, error: null,
    })
    const w = mountPage()
    await flushPromises()
    await findBtn(w, '测试').trigger('click')
    await flushPromises()
    expect(adminApi.testWebhook).toHaveBeenCalledWith(1)
  })

  it('deliveries tab lists rows with status tags', async () => {
    vi.mocked(adminApi.listWebhookDeliveries).mockResolvedValue(deliveries)
    const w = mountPage()
    await flushPromises()
    expect(adminApi.listWebhookDeliveries).not.toHaveBeenCalled() // 惰性:未切页签不查
    await w
      .findAll('.el-tabs__item')
      .find((t) => t.text() === '投递记录')!
      .trigger('click')
    await flushPromises()
    expect(adminApi.listWebhookDeliveries).toHaveBeenCalled()
    const rows = w.findAll('.d-table .el-table__row')
    const deadRow = rows.find((r) => r.text().includes('面板端点'))!
    expect(deadRow.find('.el-tag--danger')).toBeTruthy()
    const retryRow = rows.find((r) => r.text().includes('备份端点'))!
    expect(retryRow.find('.el-tag--warning')).toBeTruthy()
  })
})
