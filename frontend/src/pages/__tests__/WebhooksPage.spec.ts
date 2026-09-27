import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import ElementPlus, { ElSwitch, ElTooltip } from 'element-plus'
import WebhooksPage from '@/pages/WebhooksPage.vue'
import { adminApi, type WebhookDeliveryResponse, type WebhookEndpoint } from '@/api/admin'
import { kbApi } from '@/api/kb'

vi.mock('@/api/admin', () => ({
  adminApi: {
    listWebhooks: vi.fn(),
    createWebhook: vi.fn(),
    updateWebhook: vi.fn(),
    deleteWebhook: vi.fn(),
    testWebhook: vi.fn(),
    listWebhookDeliveries: vi.fn(),
    redeliverWebhook: vi.fn(),
  },
}))
vi.mock('@/api/kb', () => ({ kbApi: { list: vi.fn() } }))

const eps: WebhookEndpoint[] = [
  {
    id: 1, name: '面板端点', url: 'https://panel.example.com/hook',
    events: ['document.done', 'eval.completed'], enabled: true,
    description: null, secret_masked: 'wh_****ab12',
    created_at: '2026-09-22T10:00:00',
    // 反证字段:后端 list 永不回传明文,页面若误渲染 row.secret 此处即露馅
    secret: 'wh_plain_SECRET_XYZ',
    // M18:平台 / KB 订阅范围 / 投递统计(list 聚合返回)
    provider: 'generic',
    im_secret_set: true, // M20:generic 行不消费此字段,仅为满足接口
    kb_ids: [3],
    stats: {
      total: 42, succeeded: 40, pending: 0, retrying: 1, dead: 1,
      last_activity_at: '2026-09-22T12:00:00',
    },
  } as WebhookEndpoint,
  {
    id: 2, name: '备份端点', url: 'https://backup.example.com/hook',
    events: null, enabled: false,
    description: null, secret_masked: 'wh_****cd34',
    created_at: '2026-09-22T11:00:00',
    provider: 'wecom', im_secret_set: false, kb_ids: null, stats: null,
  },
  {
    // M20:钉钉平台通道两态——secret 列用 im_secret_set 显状态(此处未设置)
    id: 3, name: '钉钉告警', url: 'https://oapi.dingtalk.com/robot/send?access_token=a1',
    events: null, enabled: true,
    description: null, secret_masked: 'wh_****ef56',
    created_at: '2026-09-23T09:00:00',
    provider: 'dingtalk', kb_ids: null, stats: null,
    im_secret_set: false,
  } as WebhookEndpoint,
  {
    // M20:钉钉平台通道(已设置加签密钥)
    id: 4, name: '钉钉值班', url: 'https://oapi.dingtalk.com/robot/send?access_token=b2',
    events: null, enabled: true,
    description: null, secret_masked: 'wh_****ef78',
    created_at: '2026-09-23T10:00:00',
    provider: 'dingtalk', kb_ids: null, stats: null,
    im_secret_set: true,
  } as WebhookEndpoint,
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
    {
      // M18:成功行——重投按钮只应出现在 dead / retrying 行
      id: 13, endpoint_id: 1, endpoint_name: '已成功', event_type: 'chat.refused',
      status: 'succeeded', attempts: 1, response_status: 200,
      last_error: null, payload: null, next_attempt_at: null,
      created_at: '2026-09-22T12:02:00',
    },
  ],
}

const mountPage = () => mount(WebhooksPage, { global: { plugins: [ElementPlus] } })
// 精确匹配:工具行「新建端点」/操作列「测试」等与对话框按钮须以全文相等区分;
// 先断言存在再返回,失败时给出可读信息而非裸 undefined 崩溃
const findBtn = (w: ReturnType<typeof mount>, text: string) => {
  const btn = w.findAll('button').find((b) => b.text().trim() === text)
  expect(btn, `button「${text}」未找到`).toBeTruthy()
  return btn!
}

describe('WebhooksPage', () => {
  beforeEach(() => {
    vi.mocked(adminApi.listWebhooks).mockReset()
    vi.mocked(adminApi.createWebhook).mockReset()
    vi.mocked(adminApi.updateWebhook).mockReset()
    vi.mocked(adminApi.deleteWebhook).mockReset()
    vi.mocked(adminApi.testWebhook).mockReset()
    vi.mocked(adminApi.listWebhookDeliveries).mockReset()
    vi.mocked(adminApi.redeliverWebhook).mockReset()
    vi.mocked(kbApi.list).mockReset()
    vi.mocked(kbApi.list).mockResolvedValue([{ id: 3, name: 'KB甲' }] as never)
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
      provider: 'generic', // M18:kb_ids 空时不带键
    })
    await vi.waitFor(() => {
      // 一次性弹窗:明文 + 「仅此一次」警示文案
      expect(w.text()).toContain('wh_plainsecret123')
      expect(w.text()).toContain('仅此一次')
    })
  })

  it('edit dialog prefills row values after a prior create-dialog open', async () => {
    const w = mountPage()
    await flushPromises()
    // 先开一次新建(空表单挂载,form-item 记下空快照)再关掉:
    // el-dialog 内容跨关闭持久,若 openEdit 误用 resetFields 会把预填清回挂载快照
    await findBtn(w, '新建端点').trigger('click')
    await flushPromises()
    await findBtn(w, '取消').trigger('click')
    await flushPromises()
    await findBtn(w, '编辑').trigger('click')
    await flushPromises()
    expect(w.text()).toContain('编辑端点')
    const nameInput = w.find('input[placeholder="请输入端点名称"]')
    expect((nameInput.element as HTMLInputElement).value).toBe('面板端点')
    const urlInput = w.find('input[placeholder="https://example.com/webhook"]')
    expect((urlInput.element as HTMLInputElement).value).toBe(
      'https://panel.example.com/hook',
    )
  })

  it('编辑清空描述:提交 payload 携带空串(后端 "" → NULL 清空,F6)', async () => {
    vi.mocked(adminApi.updateWebhook).mockResolvedValue({
      ...eps[0]!, description: null,
    })
    const w = mountPage()
    await flushPromises()
    await findBtn(w, '编辑').trigger('click')
    await flushPromises()
    const desc = w.find('input[placeholder="选填,用途备注"]')
    await desc.setValue('临时备注')
    await desc.setValue('') // 清空输入框
    await findBtn(w, '保存').trigger('click')
    await flushPromises()
    // 恰为空串而非缺键:'' || undefined 会丢键,后端按「未发送 → 不变」处理,清空失效
    expect(adminApi.updateWebhook).toHaveBeenCalledWith(
      1,
      expect.objectContaining({ description: '' }),
    )
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

  // ---- M18 ----

  it('provider 联动:wecom 隐藏 secret 输入,dingtalk 显示加签密钥', async () => {
    const w = mountPage()
    await flushPromises()
    await findBtn(w, '新建端点').trigger('click')
    await flushPromises()
    const sel = w.getComponent('.provider-select') as never as {
      vm: { $emit: (e: string, v: unknown) => void }
    }
    expect(w.find('input[placeholder="留空自动生成"]').exists()).toBe(true) // generic
    ;(sel.vm as never as { $emit: (e: string, v: unknown) => void })
      .$emit('update:modelValue', 'wecom')
    await flushPromises()
    expect(w.find('input[placeholder="留空自动生成"]').exists()).toBe(false)
    ;(sel.vm as never as { $emit: (e: string, v: unknown) => void })
      .$emit('update:modelValue', 'dingtalk')
    await flushPromises()
    expect(w.find(
      'input[placeholder="平台机器人加签密钥,未开启加签可留空"]').exists()).toBe(true)
  })

  it('KB 多选:提交 payload 携带 kb_ids', async () => {
    vi.mocked(adminApi.createWebhook).mockResolvedValue({
      ...eps[0]!, id: 9, secret: 'wh_plainsecret123',
    } as never)
    const w = mountPage()
    await flushPromises()
    await findBtn(w, '新建端点').trigger('click')
    await flushPromises()
    await w.find('input[placeholder="请输入端点名称"]').setValue('KB端点')
    await w
      .find('input[placeholder="https://example.com/webhook"]')
      .setValue('https://kb.example.com/hook')
    ;(w.getComponent('.kb-select') as never as {
      vm: { $emit: (e: string, v: unknown) => void } }).vm.$emit('update:modelValue', [3])
    await findBtn(w, '保存').trigger('click')
    await flushPromises()
    expect(adminApi.createWebhook).toHaveBeenCalledWith({
      name: 'KB端点',
      url: 'https://kb.example.com/hook',
      events: [],
      provider: 'generic',
      kb_ids: [3],
    })
  })

  it('统计列渲染 stats 总数', async () => {
    const w = mountPage()
    await flushPromises()
    const row = w.findAll('.ep-table .el-table__row')
      .find((r) => r.text().includes('面板端点'))!
    expect(row.text()).toContain('42') // stats.total(eps[0] 夹具)
  })

  it('重投:dead/retrying 行显示按钮并调用 API;succeeded 行不显示', async () => {
    vi.mocked(adminApi.listWebhookDeliveries).mockResolvedValue(deliveries)
    vi.mocked(adminApi.redeliverWebhook)
      .mockResolvedValue({ status: 'pending', delivery_id: 11 })
    const w = mountPage()
    await flushPromises()
    await w.findAll('.el-tabs__item')
      .find((t) => t.text() === '投递记录')!.trigger('click')
    await flushPromises()
    const rows = w.findAll('.d-table .el-table__row')
    const deadRow = rows.find((r) => r.text().includes('面板端点'))! // dead(id 11)
    const okRow = rows.find((r) => r.text().includes('已成功'))! // succeeded(id 13)
    const btn = deadRow.findAll('button').find((b) => b.text().trim() === '重投')
    expect(btn).toBeTruthy()
    expect(okRow.findAll('button').find((b) => b.text().trim() === '重投'))
      .toBeFalsy()
    const { ElMessageBox } = await import('element-plus')
    vi.spyOn(ElMessageBox, 'confirm').mockResolvedValue(undefined as never)
    await btn!.trigger('click')
    await flushPromises()
    expect(adminApi.redeliverWebhook).toHaveBeenCalledWith(1, 11)
  })

  it('投递筛选变更触发重查', async () => {
    vi.mocked(adminApi.listWebhookDeliveries).mockResolvedValue(deliveries)
    const w = mountPage()
    await flushPromises()
    await w.findAll('.el-tabs__item')
      .find((t) => t.text() === '投递记录')!.trigger('click')
    await flushPromises()
    expect(adminApi.listWebhookDeliveries).toHaveBeenCalledTimes(1)
    ;(w.getComponent('.status-filter') as never as {
      vm: { $emit: (e: string, v: unknown) => void } }).vm.$emit('update:modelValue', 'dead')
    await flushPromises()
    expect(adminApi.listWebhookDeliveries).toHaveBeenCalledTimes(2)
    const lastCall = vi.mocked(adminApi.listWebhookDeliveries).mock.calls[1]![0]!
    expect(lastCall.status).toBe('dead')
    expect(lastCall.page).toBe(1)
  })

  it('copySecret 失败降级提示', async () => {
    vi.mocked(adminApi.createWebhook).mockResolvedValue({
      ...eps[0]!, id: 9, secret: 'wh_plainsecret123',
    } as never)
    const { ElMessage } = await import('element-plus')
    const warn = vi.spyOn(ElMessage, 'warning')
    const w = mountPage()
    await flushPromises()
    await findBtn(w, '新建端点').trigger('click')
    await flushPromises()
    await w.find('input[placeholder="请输入端点名称"]').setValue('cp')
    await w.find('input[placeholder="https://example.com/webhook"]')
      .setValue('https://cp.example.com/hook')
    await findBtn(w, '保存').trigger('click')
    await vi.waitFor(() => expect(w.text()).toContain('wh_plainsecret123'))
    const clip = { writeText: vi.fn().mockRejectedValue(new Error('denied')) }
    vi.stubGlobal('navigator', { clipboard: clip })
    await findBtn(w, '复制').trigger('click')
    await flushPromises()
    expect(clip.writeText).toHaveBeenCalled()
    expect(warn).toHaveBeenCalledWith(expect.stringContaining('剪贴板不可用'))
    vi.unstubAllGlobals()
  })

  // ---- M19 T6 ----

  it('范围列 tooltip 显示消歧后的 KB 名;全库行无 tooltip', async () => {
    vi.mocked(kbApi.list).mockResolvedValue([
      { id: 3, name: 'KB甲' }, { id: 9, name: 'KB乙' }] as never)
    vi.mocked(adminApi.listWebhooks).mockResolvedValue([
      { ...eps[0]!, kb_ids: [3, 9] },
      eps[1]!, // kb_ids=null → 「全部」无 tooltip
    ] as never)
    const w = mountPage()
    await flushPromises()
    const rows = w.findAll('.ep-table .el-table__row')
    expect(rows.find((r) => r.text().includes('面板端点'))!.text())
      .toContain('2 库')
    expect(rows.find((r) => r.text().includes('备份端点'))!.text())
      .toContain('全部')
    // 统计列也有 tooltip:按内容含 KB 名筛出范围列那个
    const tip = w.findAllComponents(ElTooltip)
      .find((t) => String(t.props('content') ?? '').includes('KB甲'))
    expect(tip).toBeTruthy()
    expect(tip!.props('content')).toBe('KB甲\nKB乙')
  })

  it('wecom 编辑无轮换开关;secret 列显「无需密钥」', async () => {
    const w = mountPage()
    await flushPromises()
    const rows = w.findAll('.ep-table .el-table__row')
    expect(rows.find((r) => r.text().includes('备份端点'))!.text())
      .toContain('无需密钥') // wecom 行灰字占位
    expect(rows.find((r) => r.text().includes('面板端点'))!.text())
      .toContain('wh_****ab12') // 非 wecom 行仍 masked
    const editBtns = w.findAll('button')
      .filter((b) => b.text().trim() === '编辑')
    await editBtns[1]!.trigger('click') // 第二行 = wecom(eps[1])
    await flushPromises()
    expect(w.text()).toContain('编辑端点')
    expect(w.text()).not.toContain('轮换密钥') // generic-only
    // 对照:generic 端点编辑仍显示轮换开关
    await w.findAll('button')
      .filter((b) => b.text().trim() === '取消')[0]!.trigger('click')
    await flushPromises()
    await w.findAll('button')
      .filter((b) => b.text().trim() === '编辑')[0]!.trigger('click')
    await flushPromises()
    expect(w.text()).toContain('轮换密钥')
  })

  it('编辑提交:响应带 secret 即弹一次性弹窗(切到 generic,未开 rotate)', async () => {
    // M19:PUT 把 provider 切到 generic 时后端返回 WebhookCreatedOut(新明文一次);
    // rotate 只是带 secret 的子集——凡响应带 secret 都必须进弹窗,否则明文静默丢失
    vi.mocked(adminApi.updateWebhook).mockResolvedValue({
      ...eps[0]!, provider: 'generic', secret: 'wh_newswitchsecret1',
    })
    const w = mountPage()
    await flushPromises()
    await findBtn(w, '编辑').trigger('click')
    await flushPromises()
    await findBtn(w, '保存').trigger('click')
    await flushPromises()
    // rotate 开关未动:payload 明确 rotate_secret: false,新明文仍须弹出
    expect(adminApi.updateWebhook).toHaveBeenCalledWith(
      1,
      expect.objectContaining({ rotate_secret: false, provider: 'generic' }),
    )
    await vi.waitFor(() => {
      expect(w.text()).toContain('wh_newswitchsecret1')
      expect(w.text()).toContain('仅此一次')
    })
  })

  // ---- M20:eval.cancelled 事件选项 ----

  it('事件选项含「评估已取消」(eval.cancelled)', async () => {
    const w = mountPage()
    await flushPromises()
    await findBtn(w, '新建端点').trigger('click')
    await flushPromises()
    // el-select 选项 dropdown 经 teleport 挂 document.body;投递记录筛选下拉是
    // 超集(含「测试」),以含「评估失败」且不含「测试」锚定新建弹窗的事件订阅
    // dropdown,经渲染文本断言(EVENT_OPTIONS 为 SFC 内常量)
    const dropdown = Array.from(document.querySelectorAll('.el-select-dropdown'))
      .find((d) => d.textContent?.includes('评估失败')
        && !d.textContent?.includes('测试'))
    expect(dropdown, '事件订阅下拉未渲染').toBeTruthy()
    const labels = Array
      .from(dropdown!.querySelectorAll('.el-select-dropdown__item'))
      .map((li) => li.textContent?.trim() ?? '')
    expect(labels).toHaveLength(6)
    expect(labels).toContain('评估已取消')
  })

  // ---- M20:平台通道 secret 列显 im_secret_set 状态 ----

  it('平台通道密钥列:im_secret_set 显「未设置/已设置」', async () => {
    const w = mountPage()
    await flushPromises()
    const rows = w.findAll('.ep-table .el-table__row')
    // 未设置:文本 + 灰字类 no-secret
    const unset = rows.find((r) => r.text().includes('钉钉告警'))!
    expect(unset.text()).toContain('未设置')
    // 空结果直接 expect 会触发 VTU 空 wrapper 代理异常,须经 exists() 断言
    expect(unset.find('.no-secret').exists()).toBe(true)
    // 已设置:文本在、且不落灰字类
    const set = rows.find((r) => r.text().includes('钉钉值班'))!
    expect(set.text()).toContain('已设置')
    expect(set.find('.no-secret').exists()).toBe(false)
    // 对照:三分支其余两支不变——generic 仍 masked、wecom 仍「无需密钥」
    expect(rows.find((r) => r.text().includes('面板端点'))!.text())
      .toContain('wh_****ab12')
    expect(rows.find((r) => r.text().includes('备份端点'))!.text())
      .toContain('无需密钥')
  })
})
