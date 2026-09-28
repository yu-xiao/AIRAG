<script setup lang="ts">
import { onMounted, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox, type FormInstance, type FormRules } from 'element-plus'
import PageHeader from '@/components/PageHeader.vue'
import {
  adminApi,
  type WebhookDeliveryRow,
  type WebhookEndpoint,
  type WebhookProvider,
  type WebhookStats,
} from '@/api/admin'
import { kbApi } from '@/api/kb'
import { disambiguateKbNames } from '@/utils/kbLabel'

const tab = ref<'endpoints' | 'deliveries'>('endpoints')

// 六事件(后端 outbound.EVENT_TYPES);test 仅出现在投递记录(测试发送专用)
const EVENT_OPTIONS = [
  { value: 'document.done', label: '文档解析完成' },
  { value: 'document.failed', label: '文档解析失败' },
  { value: 'eval.completed', label: '评估完成' },
  { value: 'eval.failed', label: '评估失败' },
  { value: 'eval.cancelled', label: '评估已取消' },
  { value: 'chat.refused', label: '对话拒答' },
]
const DELIVERY_EVENT_OPTIONS = [...EVENT_OPTIONS, { value: 'test', label: '测试' }]
const EVENT_LABEL: Record<string, string> = Object.fromEntries(
  DELIVERY_EVENT_OPTIONS.map((e) => [e.value, e.label]),
)
const DELIVERY_STATUS: Record<
  string,
  { label: string; type: 'info' | 'warning' | 'success' | 'danger' }
> = {
  pending: { label: '待投递', type: 'info' },
  retrying: { label: '重试中', type: 'warning' },
  succeeded: { label: '成功', type: 'success' },
  dead: { label: '已放弃', type: 'danger' },
}

// M18:平台适配——generic = 自签 HMAC 密钥;wecom = 无密钥;钉钉/飞书 = 平台加签密钥
const PROVIDER_OPTIONS = [
  { value: 'generic', label: '通用(JSON+签名)', secretMode: 'generic' },
  { value: 'wecom', label: '企业微信', secretMode: 'none' },
  { value: 'dingtalk', label: '钉钉', secretMode: 'platform' },
  { value: 'feishu', label: '飞书', secretMode: 'platform' },
] as const
const PROVIDER_LABEL: Record<string, string> = Object.fromEntries(
  PROVIDER_OPTIONS.map((p) => [p.value, p.label]),
)
const URL_PLACEHOLDER: Record<string, string> = {
  generic: 'https://example.com/webhook',
  wecom: 'https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=...',
  dingtalk: 'https://oapi.dingtalk.com/robot/send?access_token=...',
  feishu: 'https://open.feishu.cn/open-apis/bot/v2/hook/...',
}
// M21:按通道联动的引导——generic 只看 HTTP 状态码,企微业务错误也回 200,
// 用 generic 接企微会误报成功(09-28 走查口头确认的坑)
const PROVIDER_HELP: Record<string, string> = {
  generic: '通用通道:自签 HMAC 密钥,成功判定只看 HTTP 状态码(2xx 即成功)',
  wecom: '企微群机器人请用本通道(URL 取「群设置→群机器人→新建→复制 Webhook」),无需密钥。注意:通用通道只看 HTTP 状态码,企微业务错误也回 200,用通用通道接企微会误报成功',
  dingtalk: '钉钉自定义机器人;机器人安全设置中的加签密钥可选(填入后按平台方式加签)',
  feishu: '飞书自定义机器人;机器人安全设置中的签名密钥可选(填入后按平台方式加签)',
}

// KB 订阅范围选项(admin 可见全部 KB);加载失败静默——订阅选填,不打断端点页
const kbOptions = ref<{ id: number; name: string }[]>([])

function eventLabel(e: string) {
  return EVENT_LABEL[e] ?? e
}

const errMsg = (e: unknown, fallback: string) =>
  (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? fallback

function fmtTime(iso: string) {
  return iso.replace('T', ' ').slice(0, 19)
}

/** M18:统计列 tooltip 明细 */
function statsTooltip(s: WebhookStats): string {
  return `成功 ${s.succeeded} · 重试 ${s.retrying} · 待投 ${s.pending} · 死信 ${s.dead} · 最近 ${
    s.last_activity_at ? fmtTime(s.last_activity_at) : '—'
  }`
}

/** M19 T6:范围列 tooltip——KB 名经重名消歧后的 \n 列表 */
function scopeTooltip(kbIds: number[] | null): string {
  const labels = disambiguateKbNames(kbOptions.value)
  return (kbIds ?? []).map((id) => labels.get(id) ?? `#${id}`).join('\n')
}

// ---- 端点管理 ----

const loading = ref(false)
const endpoints = ref<WebhookEndpoint[]>([])

async function loadEndpoints() {
  loading.value = true
  try {
    endpoints.value = await adminApi.listWebhooks()
  } catch (e) {
    ElMessage.error(errMsg(e, '端点列表加载失败'))
  } finally {
    loading.value = false
  }
}

const dialogVisible = ref(false)
const submitting = ref(false)
const editing = ref<WebhookEndpoint | null>(null) // null = 新建
const formRef = ref<FormInstance>()
const form = reactive({
  name: '',
  url: '',
  description: '',
  events: [] as string[], // 空 = 订阅全部(后端语义)
  secret: '', // 新建可选:generic 自动生成留空;钉钉/飞书 = 平台加签密钥
  rotate: false, // 编辑时轮换密钥
  provider: 'generic' as WebhookProvider, // M18:平台类型
  kbIds: [] as number[], // M18:空 = 订阅全部知识库
})
const rules: FormRules = {
  name: [{ required: true, message: '请输入端点名称', trigger: 'blur' }],
  url: [
    { required: true, message: '请输入回调 URL', trigger: 'blur' },
    { pattern: /^https?:\/\//, message: '需以 http(s):// 开头', trigger: 'blur' },
  ],
}

// secret 一次性明文:仅 create / rotate / 切换到 generic 响应携带,关闭即弃
const oneTimeSecret = ref<string | null>(null)

// 不用 resetFields:其恢复的是 form-item 挂载时快照,而 el-dialog 内容跨关闭持久
// (首开为新建则编辑预填被清空,首开为编辑则新建带旧值)。表单为组件自有状态,
// 每次 open 全字段显式赋值,只需 clearValidate 清上一次残留的校验红字。
function openCreate() {
  editing.value = null
  form.name = ''
  form.url = ''
  form.description = ''
  form.events = []
  form.secret = ''
  form.rotate = false
  form.provider = 'generic'
  form.kbIds = []
  formRef.value?.clearValidate()
  dialogVisible.value = true
}

function openEdit(row: WebhookEndpoint) {
  editing.value = row
  form.name = row.name
  form.url = row.url
  form.description = row.description ?? ''
  form.events = row.events ? [...row.events] : []
  form.secret = ''
  form.rotate = false
  form.provider = row.provider
  form.kbIds = row.kb_ids ? [...row.kb_ids] : []
  formRef.value?.clearValidate()
  dialogVisible.value = true
}

async function submit(formEl: FormInstance | undefined) {
  if (!formEl) return
  const valid = await formEl.validate().catch(() => false)
  if (!valid) return
  submitting.value = true
  try {
    if (editing.value) {
      // 简化:全量带(name/url/events/enabled/description/provider/kb_ids + rotate_secret)
      const r = await adminApi.updateWebhook(editing.value.id, {
        name: form.name,
        url: form.url,
        events: [...form.events],
        enabled: editing.value.enabled,
        description: form.description, // 空串照发:后端 "" → NULL,清空描述(F6)
        rotate_secret: form.rotate,
        provider: form.provider,
        kb_ids: [...(form.kbIds ?? [])], // [] = 订阅全部,语义等价 null
      })
      // rotate 是「响应带 secret」的子集;provider 切到 generic 同样返回新明文
      // (后端 WebhookCreatedOut),凡带 secret 都进一次性弹窗,否则明文永久丢失
      if ('secret' in r && r.secret) oneTimeSecret.value = r.secret
      else ElMessage.success('已保存')
    } else {
      const payload: {
        name: string
        url: string
        events: string[]
        description?: string
        secret?: string
        provider: WebhookProvider
        kb_ids?: number[]
      } = {
        name: form.name,
        url: form.url,
        events: [...form.events],
        provider: form.provider,
      }
      if (form.description) payload.description = form.description
      // secret:generic = 签名密钥;钉钉/飞书 = 平台加签密钥(后端按 provider 解释)
      if (form.secret && form.provider !== 'wecom') payload.secret = form.secret
      // kb_ids 空时不带键(= 订阅全部);EP 多选清空时值可能变 undefined,按 length 兜底
      const kbIds = form.kbIds ?? []
      if (kbIds.length) payload.kb_ids = [...kbIds]
      const r = await adminApi.createWebhook(payload)
      // 正常契约必有 secret;空值守卫与 rotate 路径对齐,防御异常响应
      if (r.secret) oneTimeSecret.value = r.secret
      else ElMessage.success('端点已创建')
    }
    dialogVisible.value = false
    await loadEndpoints()
  } catch (e) {
    ElMessage.error(errMsg(e, '保存失败'))
  } finally {
    submitting.value = false
  }
}

// 直切开关::model-value 单向绑定,落库成功后本地同步,失败不改动
async function toggleEnabled(row: WebhookEndpoint, v: string | number | boolean) {
  try {
    await adminApi.updateWebhook(row.id, { enabled: Boolean(v) })
    row.enabled = Boolean(v)
    ElMessage.success(v ? '已启用' : '已停用')
  } catch (e) {
    ElMessage.error(errMsg(e, '状态切换失败'))
  }
}

const testing = ref<number | null>(null)

async function runTest(row: WebhookEndpoint) {
  testing.value = row.id
  try {
    const r = await adminApi.testWebhook(row.id)
    if (r.status === 'succeeded') ElMessage.success(`投递 ${r.status}`)
    else ElMessage.error(`投递 ${r.status}${r.error ? `:${r.error}` : ''}`)
  } catch (e) {
    ElMessage.error(errMsg(e, '测试投递失败'))
  } finally {
    testing.value = null
  }
}

async function remove(row: WebhookEndpoint) {
  try {
    await ElMessageBox.confirm(
      `确定删除「${row.name}」?该端点将立即停推,历史投递记录保留。`,
      '删除端点',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  try {
    await adminApi.deleteWebhook(row.id)
    ElMessage.success('已删除')
    await loadEndpoints()
  } catch (e) {
    ElMessage.error(errMsg(e, '删除失败'))
  }
}

async function copySecret() {
  if (!oneTimeSecret.value) return
  try {
    await navigator.clipboard.writeText(oneTimeSecret.value)
    ElMessage.success('已复制到剪贴板')
  } catch {
    // 非安全上下文 / 权限拒绝:key-value 已 user-select:all,降级为手动复制
    ElMessage.warning('剪贴板不可用,请点击密钥文本手动复制(Ctrl+C)')
  }
}

// ---- 投递记录(首次切入页签时惰性加载) ----

const deliveriesLoading = ref(false)
const deliveriesLoaded = ref(false)
const deliveryRows = ref<WebhookDeliveryRow[]>([])
const deliveryTotal = ref(0)
const dQuery = reactive({
  endpointId: null as number | null,
  eventType: '',
  status: '',
  page: 1,
  pageSize: 20,
})

async function loadDeliveries() {
  deliveriesLoading.value = true
  try {
    const r = await adminApi.listWebhookDeliveries({
      endpoint_id: dQuery.endpointId ?? undefined,
      event_type: dQuery.eventType || undefined,
      status: dQuery.status || undefined,
      page: dQuery.page,
      page_size: dQuery.pageSize,
    })
    deliveryRows.value = r.items
    deliveryTotal.value = r.total
    deliveriesLoaded.value = true
  } catch (e) {
    ElMessage.error(errMsg(e, '投递记录加载失败'))
  } finally {
    deliveriesLoading.value = false
  }
}

function searchDeliveries() {
  dQuery.page = 1
  loadDeliveries()
}

// M18:筛选变更即时重查(查询按钮保留;任何筛选变化都回到第 1 页)
watch(
  [() => dQuery.endpointId, () => dQuery.eventType, () => dQuery.status],
  () => searchDeliveries(),
)

/** M18:手动重投 dead/retrying 投递 */
async function redeliver(row: WebhookDeliveryRow) {
  try {
    await ElMessageBox.confirm(
      `重投「${row.endpoint_name}」的${eventLabel(row.event_type)}投递?将立即重新发送。`,
      '手动重投',
      { type: 'warning', confirmButtonText: '重投', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  try {
    await adminApi.redeliverWebhook(row.endpoint_id, row.id)
    ElMessage.success('已重新排队投递')
    await loadDeliveries()
  } catch (e) {
    ElMessage.error(errMsg(e, '重投失败'))
  }
}

watch(tab, (t) => {
  if (t === 'deliveries' && !deliveriesLoaded.value) loadDeliveries()
})

onMounted(() => {
  void Promise.all([
    loadEndpoints(),
    // KB 列表失败静默:订阅范围选填,别让 KB 加载失败打断端点页
    kbApi
      .list()
      .then((r) => {
        kbOptions.value = r.map((k) => ({ id: k.id, name: k.name }))
      })
      .catch(() => {}),
  ])
})
</script>

<template>
  <div class="page">
    <PageHeader
      title="出站推送"
      description="向外部系统推送文档 / 评估 / 拒答事件的 Webhook 端点管理与投递记录"
    />

    <el-tabs v-model="tab" class="webhook-tabs">
      <el-tab-pane label="端点管理" name="endpoints">
        <div class="toolbar">
          <el-button type="primary" @click="openCreate">新建端点</el-button>
        </div>
        <el-table v-loading="loading" :data="endpoints" class="ep-table">
          <template #empty>
            <el-empty description="还没有端点,新建一个开始接收事件推送" />
          </template>
          <el-table-column prop="name" label="名称" min-width="120" />
          <el-table-column label="URL" min-width="220" show-overflow-tooltip>
            <template #default="{ row }">
              <span class="mono">{{ row.url }}</span>
            </template>
          </el-table-column>
          <el-table-column label="平台" min-width="120">
            <template #default="{ row }">
              {{ PROVIDER_LABEL[row.provider] ?? row.provider }}
            </template>
          </el-table-column>
          <el-table-column label="订阅" min-width="170">
            <template #default="{ row }">
              <template v-if="row.events?.length">
                <el-tag v-for="e in row.events" :key="e" size="small" class="evt-tag">
                  {{ eventLabel(e) }}
                </el-tag>
              </template>
              <el-tag v-else size="small" type="info">全部</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="范围" width="80" align="center">
            <template #default="{ row }">
              <!-- 指定范围时 tooltip 给出 KB 名单(消歧后);空 = 全部,无 tooltip -->
              <el-tooltip
                v-if="row.kb_ids?.length"
                effect="dark"
                placement="top"
                popper-class="scope-tip"
                :content="scopeTooltip(row.kb_ids)"
              >
                <span>{{ row.kb_ids.length }} 库</span>
              </el-tooltip>
              <span v-else>全部</span>
            </template>
          </el-table-column>
          <el-table-column label="状态" width="80" align="center">
            <template #default="{ row }">
              <el-switch
                :model-value="row.enabled"
                @change="(v: string | number | boolean) => toggleEnabled(row, v)"
              />
            </template>
          </el-table-column>
          <el-table-column label="secret" min-width="120">
            <template #default="{ row }">
              <!-- wecom 平台无密钥概念:灰字占位,避免 masked 误导 -->
              <span v-if="row.provider === 'wecom'" class="no-secret">无需密钥</span>
              <!-- M20:平台通道真实签名密钥在 im_secret 位,secret 列是占位——
                   用 im_secret_set 显状态,避免 masked 占位误导 -->
              <span v-else-if="row.provider !== 'generic'"
                    :class="{ 'no-secret': !row.im_secret_set }">
                {{ row.im_secret_set ? '已设置' : '未设置' }}
              </span>
              <span v-else class="mono">{{ row.secret_masked }}</span>
            </template>
          </el-table-column>
          <el-table-column label="统计" width="70" align="center">
            <template #default="{ row }">
              <el-tooltip
                v-if="row.stats"
                effect="dark"
                placement="top"
                :content="statsTooltip(row.stats)"
              >
                <span>{{ row.stats.total }}</span>
              </el-tooltip>
              <span v-else>0</span>
            </template>
          </el-table-column>
          <el-table-column label="操作" width="170" align="center">
            <template #default="{ row }">
              <el-button
                link
                type="primary"
                :loading="testing === row.id"
                @click="runTest(row)"
              >测试</el-button>
              <el-button link type="primary" @click="openEdit(row)">编辑</el-button>
              <el-button link type="danger" @click="remove(row)">删除</el-button>
            </template>
          </el-table-column>
        </el-table>
      </el-tab-pane>

      <el-tab-pane label="投递记录" name="deliveries">
        <div class="filters">
          <el-select
            v-model="dQuery.endpointId"
            placeholder="端点"
            clearable
            filterable
            class="filter-select"
          >
            <el-option v-for="ep in endpoints" :key="ep.id" :label="ep.name" :value="ep.id" />
          </el-select>
          <el-select v-model="dQuery.eventType" placeholder="事件" clearable class="filter-select">
            <el-option
              v-for="e in DELIVERY_EVENT_OPTIONS"
              :key="e.value"
              :label="e.label"
              :value="e.value"
            />
          </el-select>
          <el-select
            v-model="dQuery.status"
            placeholder="状态"
            clearable
            class="filter-select status-filter"
          >
            <el-option
              v-for="(s, key) in DELIVERY_STATUS"
              :key="key"
              :label="s.label"
              :value="key"
            />
          </el-select>
          <el-button type="primary" @click="searchDeliveries">查询</el-button>
        </div>
        <el-table v-loading="deliveriesLoading" :data="deliveryRows" class="d-table">
          <template #empty>
            <el-empty description="暂无投递记录" />
          </template>
          <el-table-column label="时间" width="170">
            <template #default="{ row }">{{ fmtTime(row.created_at) }}</template>
          </el-table-column>
          <el-table-column prop="endpoint_name" label="端点" min-width="120" />
          <el-table-column label="事件" width="120">
            <template #default="{ row }">
              <el-tag size="small">{{ eventLabel(row.event_type) }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="状态" width="90" align="center">
            <template #default="{ row }">
              <el-tag size="small" :type="DELIVERY_STATUS[row.status]?.type ?? 'info'">
                {{ DELIVERY_STATUS[row.status]?.label ?? row.status }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="attempts" label="次数" width="70" align="center" />
          <el-table-column label="响应码" width="80" align="center">
            <template #default="{ row }">{{ row.response_status ?? '—' }}</template>
          </el-table-column>
          <el-table-column label="下次尝试" width="170">
            <template #default="{ row }">
              {{ row.next_attempt_at ? fmtTime(row.next_attempt_at) : '—' }}
            </template>
          </el-table-column>
          <el-table-column label="最后错误" min-width="200" show-overflow-tooltip>
            <template #default="{ row }">{{ row.last_error ?? '—' }}</template>
          </el-table-column>
          <el-table-column label="操作" width="70" align="center">
            <template #default="{ row }">
              <!-- 仅 dead/retrying 可手动重投;succeeded/pending 无意义 -->
              <el-button
                v-if="row.status === 'dead' || row.status === 'retrying'"
                link
                type="primary"
                size="small"
                @click="redeliver(row)"
                >重投</el-button
              >
            </template>
          </el-table-column>
        </el-table>
        <el-pagination
          v-model:current-page="dQuery.page"
          v-model:page-size="dQuery.pageSize"
          class="d-pagination"
          layout="total, prev, pager, next, sizes"
          :page-sizes="[20, 50, 100]"
          :total="deliveryTotal"
          @current-change="loadDeliveries"
          @size-change="searchDeliveries"
        />
      </el-tab-pane>
    </el-tabs>

    <!-- 新建 / 编辑端点 -->
    <el-dialog v-model="dialogVisible" :title="editing ? '编辑端点' : '新建端点'" width="520px">
      <el-form ref="formRef" :model="form" :rules="rules" label-position="top">
        <el-form-item label="名称" prop="name">
          <el-input v-model="form.name" maxlength="64" placeholder="请输入端点名称" />
        </el-form-item>
        <el-form-item label="平台类型">
          <el-select v-model="form.provider" class="provider-select">
            <el-option
              v-for="p in PROVIDER_OPTIONS"
              :key="p.value"
              :label="p.label"
              :value="p.value"
            />
          </el-select>
          <div class="form-help">{{ PROVIDER_HELP[form.provider] ?? '' }}</div>
        </el-form-item>
        <el-form-item label="回调 URL" prop="url">
          <el-input
            v-model="form.url"
            :placeholder="URL_PLACEHOLDER[form.provider] ?? 'https://example.com/webhook'"
          />
        </el-form-item>
        <el-form-item label="描述">
          <el-input v-model="form.description" maxlength="200" placeholder="选填,用途备注" />
        </el-form-item>
        <el-form-item label="事件订阅">
          <el-select v-model="form.events" multiple placeholder="不选 = 订阅全部事件">
            <el-option
              v-for="e in EVENT_OPTIONS"
              :key="e.value"
              :label="e.label"
              :value="e.value"
            />
          </el-select>
          <div class="form-help">不选择任何事件 = 订阅全部六类事件</div>
        </el-form-item>
        <el-form-item label="知识库范围">
          <el-select
            v-model="form.kbIds"
            multiple
            class="kb-select"
            placeholder="不选 = 订阅全部知识库"
          >
            <el-option v-for="k in kbOptions" :key="k.id" :label="k.name" :value="k.id" />
          </el-select>
          <div class="form-help">不选 = 订阅全部知识库</div>
        </el-form-item>
        <el-form-item v-if="!editing && form.provider === 'generic'" label="签名密钥">
          <el-input v-model="form.secret" placeholder="留空自动生成" />
        </el-form-item>
        <el-form-item
          v-else-if="!editing && form.provider !== 'wecom'"
          label="加签密钥(可选)"
        >
          <el-input
            v-model="form.secret"
            placeholder="平台机器人加签密钥,未开启加签可留空"
          />
        </el-form-item>
        <!-- 轮换仅 generic 有意义:wecom 无密钥,钉钉/飞书密钥由平台管理 -->
        <el-form-item v-if="editing && form.provider === 'generic'" label="轮换密钥">
          <el-switch v-model="form.rotate" />
          <div class="form-help">开启后保存时生成新密钥,旧密钥立即失效</div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="submitting" @click="submit(formRef)">保存</el-button>
      </template>
    </el-dialog>

    <!-- secret 一次性弹窗:明文仅此一次,关闭即弃 -->
    <el-dialog
      :model-value="oneTimeSecret !== null"
      title="密钥已生成"
      width="520px"
      :close-on-click-modal="false"
      @close="oneTimeSecret = null"
    >
      <el-alert
        type="warning"
        :closable="false"
        show-icon
        title="明文密钥仅此一次展示,请妥善保存。"
        class="alert"
      />
      <div class="key-box">
        <code class="key-value">{{ oneTimeSecret }}</code>
        <el-button @click="copySecret">复制</el-button>
      </div>
      <template #footer>
        <el-button type="primary" @click="oneTimeSecret = null">我已保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.page {
  display: flex;
  flex-direction: column;
  gap: var(--app-spacing-4, 16px);
}
.webhook-tabs {
  background: var(--el-bg-color);
  border-radius: var(--app-radius);
  padding: 0 16px 16px;
}
.toolbar {
  display: flex;
  justify-content: flex-end;
  margin-bottom: 12px;
}
.mono {
  font-family: var(--app-font-mono);
  font-size: 13px;
}
.no-secret {
  font-size: 13px;
  color: var(--el-text-color-secondary);
}
.evt-tag {
  margin-right: 4px;
}
.filters {
  display: flex;
  gap: 8px;
  margin-bottom: 12px;
}
.filter-select {
  width: 160px;
}
.d-pagination {
  margin-top: 12px;
  justify-content: flex-end;
}
.form-help {
  width: 100%;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.alert {
  margin-bottom: 12px;
}
.key-box {
  display: flex;
  align-items: center;
  gap: var(--app-spacing-sm);
}
.key-value {
  flex: 1;
  min-width: 0;
  padding: 8px 12px;
  border: 1px solid var(--app-card-border);
  border-radius: var(--app-radius-sm);
  background: var(--app-bg-soft);
  font-family: var(--app-font-mono);
  font-size: 13px;
  word-break: break-all;
  user-select: all;
}
</style>

<style>
/* tooltip popper 挂 body,scoped 够不着:KB 名单按 \n 换行 */
.scope-tip {
  white-space: pre-line;
}
</style>
