<script setup lang="ts">
import { onMounted, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox, type FormInstance, type FormRules } from 'element-plus'
import PageHeader from '@/components/PageHeader.vue'
import {
  adminApi,
  type WebhookDeliveryRow,
  type WebhookEndpoint,
} from '@/api/admin'

const tab = ref<'endpoints' | 'deliveries'>('endpoints')

// 五事件(后端 outbound.EVENT_TYPES);test 仅出现在投递记录(测试发送专用)
const EVENT_OPTIONS = [
  { value: 'document.done', label: '文档解析完成' },
  { value: 'document.failed', label: '文档解析失败' },
  { value: 'eval.completed', label: '评估完成' },
  { value: 'eval.failed', label: '评估失败' },
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

function eventLabel(e: string) {
  return EVENT_LABEL[e] ?? e
}

const errMsg = (e: unknown, fallback: string) =>
  (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? fallback

function fmtTime(iso: string) {
  return iso.replace('T', ' ').slice(0, 19)
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
  secret: '', // 新建可选,留空自动生成
  rotate: false, // 编辑时轮换密钥
})
const rules: FormRules = {
  name: [{ required: true, message: '请输入端点名称', trigger: 'blur' }],
  url: [
    { required: true, message: '请输入回调 URL', trigger: 'blur' },
    { pattern: /^https?:\/\//, message: '需以 http(s):// 开头', trigger: 'blur' },
  ],
}

// secret 一次性明文:仅 create / rotate 响应携带,关闭即弃
const oneTimeSecret = ref<string | null>(null)

function openCreate() {
  editing.value = null
  form.name = ''
  form.url = ''
  form.description = ''
  form.events = []
  form.secret = ''
  form.rotate = false
  formRef.value?.resetFields()
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
  formRef.value?.resetFields()
  dialogVisible.value = true
}

async function submit(formEl: FormInstance | undefined) {
  if (!formEl) return
  const valid = await formEl.validate().catch(() => false)
  if (!valid) return
  submitting.value = true
  try {
    if (editing.value) {
      // 简化:全量带(name/url/events/enabled/description + rotate_secret)
      const r = await adminApi.updateWebhook(editing.value.id, {
        name: form.name,
        url: form.url,
        events: [...form.events],
        enabled: editing.value.enabled,
        description: form.description || undefined,
        rotate_secret: form.rotate,
      })
      if (form.rotate && 'secret' in r && r.secret) oneTimeSecret.value = r.secret
      else ElMessage.success('已保存')
    } else {
      const payload: { name: string; url: string; events: string[]; description?: string; secret?: string } = {
        name: form.name,
        url: form.url,
        events: [...form.events],
      }
      if (form.description) payload.description = form.description
      if (form.secret) payload.secret = form.secret
      const r = await adminApi.createWebhook(payload)
      oneTimeSecret.value = r.secret
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
  await navigator.clipboard.writeText(oneTimeSecret.value)
  ElMessage.success('已复制到剪贴板')
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

watch(tab, (t) => {
  if (t === 'deliveries' && !deliveriesLoaded.value) loadDeliveries()
})

onMounted(loadEndpoints)
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
              <span class="mono">{{ row.secret_masked }}</span>
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
          <el-select v-model="dQuery.status" placeholder="状态" clearable class="filter-select">
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
        <el-form-item label="回调 URL" prop="url">
          <el-input v-model="form.url" placeholder="https://example.com/webhook" />
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
          <div class="form-help">不选择任何事件 = 订阅全部五类事件</div>
        </el-form-item>
        <el-form-item v-if="!editing" label="签名密钥">
          <el-input v-model="form.secret" placeholder="留空自动生成" />
        </el-form-item>
        <el-form-item v-if="editing" label="轮换密钥">
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
