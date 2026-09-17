<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, type UploadRawFile, type UploadRequestOptions } from 'element-plus'
import { ArrowLeft, Search } from '@element-plus/icons-vue'
import { documentsApi, type ChunksResponse, type DocumentItem } from '@/api/documents'
import { kbApi } from '@/api/kb'
import PageHeader from '@/components/PageHeader.vue'

const route = useRoute()
const router = useRouter()

const kbId = Number(route.params.id)
const kbName = ref(`知识库 #${kbId}`)
const myPerm = ref<string | null>(null)

const canEdit = computed(() => myPerm.value === 'owner' || myPerm.value === 'editor')
/** 允许触发重新解析的状态(处理中禁止重入,与后端 409 语义一致) */
const REPROCESSABLE = ['pending', 'done', 'failed']

const loading = ref(false)
const docs = ref<DocumentItem[]>([])

const MAX_UPLOAD_MB = 20
const ALLOWED_EXTS = ['.pdf', '.docx', '.xlsx', '.jpg', '.jpeg', '.png']
const NON_TERMINAL = ['pending', 'parsing', 'chunking', 'embedding']

const STATUS_META: Record<string, { label: string; type: 'info' | 'primary' | 'success' | 'danger' }> = {
  pending: { label: '待处理', type: 'info' },
  parsing: { label: '解析中', type: 'primary' },
  chunking: { label: '切分中', type: 'primary' },
  embedding: { label: '向量化中', type: 'primary' },
  done: { label: '完成', type: 'success' },
  failed: { label: '失败', type: 'danger' },
}

function statusMeta(status: string) {
  return STATUS_META[status] ?? { label: status, type: 'info' as const }
}

// ---- 工具行:搜索 / 状态筛选 / 客户端分页 ----
const keyword = ref('')
const statusFilter = ref<string>('')
const page = reactive({ page: 1, pageSize: 20 })

const processingCount = computed(
  () => docs.value.filter((d) => NON_TERMINAL.includes(d.status)).length,
)

const filteredDocs = computed(() => {
  const k = keyword.value.trim().toLowerCase()
  return docs.value.filter((d) => {
    const okKw = !k || d.filename.toLowerCase().includes(k)
    const okStatus = !statusFilter.value || d.status === statusFilter.value
    return okKw && okStatus
  })
})

const pagedDocs = computed(() =>
  filteredDocs.value.slice((page.page - 1) * page.pageSize, page.page * page.pageSize),
)

watch([keyword, statusFilter], () => {
  page.page = 1
})

// ---- 上传(手动模式:http-request 调 documentsApi.upload)----
const uploading = ref(false)
const uploadPercent = ref(0)

function beforeUpload(raw: UploadRawFile) {
  const ext = raw.name.slice(raw.name.lastIndexOf('.')).toLowerCase()
  if (!ALLOWED_EXTS.includes(ext)) {
    ElMessage.error(`不支持的文件类型 ${ext},仅支持 .pdf/.docx/.xlsx/.jpg/.png`)
    return false
  }
  if (raw.size > MAX_UPLOAD_MB * 1024 * 1024) {
    ElMessage.error(`文件超过 ${MAX_UPLOAD_MB}MB 上限`)
    return false
  }
  return true
}

async function doUpload(options: UploadRequestOptions) {
  uploading.value = true
  uploadPercent.value = 0
  try {
    const doc = await documentsApi.upload(kbId, options.file, (p) => {
      uploadPercent.value = p
    })
    ElMessage.success(`「${doc.filename}」上传成功,等待处理`)
    uploadPercent.value = 100
    await load()
  } catch (e) {
    const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail
    ElMessage.error(detail ?? '上传失败')
  } finally {
    uploading.value = false
  }
}

// ---- 列表与非终态 3s 轮询,全部终态即停 ----
let timer: number | undefined
let disposed = false

function hasActive() {
  return docs.value.some((d) => NON_TERMINAL.includes(d.status))
}

function syncPolling() {
  if (disposed) return
  if (hasActive()) {
    if (timer === undefined) timer = window.setInterval(() => load(true), 3000)
  } else if (timer !== undefined) {
    window.clearInterval(timer)
    timer = undefined
  }
}

async function load(silent = false) {
  if (disposed) return
  if (!silent) loading.value = true
  try {
    docs.value = await documentsApi.list(kbId)
  } catch {
    if (!silent) ElMessage.error('加载文档列表失败')
  } finally {
    if (!silent) loading.value = false
    syncPolling()
  }
}

// ---- 分块抽屉 ----
const drawerVisible = ref(false)
const drawerDoc = ref<DocumentItem | null>(null)
const chunksLoading = ref(false)
const chunks = ref<ChunksResponse>({ total: 0, items: [] })
const chunkPage = reactive({ page: 1, pageSize: 20 })

async function fetchChunks() {
  if (!drawerDoc.value) return
  chunksLoading.value = true
  try {
    chunks.value = await documentsApi.chunks(drawerDoc.value.id, chunkPage.page, chunkPage.pageSize)
  } catch {
    ElMessage.error('加载分块失败')
  } finally {
    chunksLoading.value = false
  }
}

function openChunks(row: DocumentItem) {
  drawerDoc.value = row
  chunkPage.page = 1
  drawerVisible.value = true
  fetchChunks()
}

// ---- 重新解析(editor+;删旧块回 pending 重新入队)----
const reprocessing = ref<number | null>(null)

async function reprocess(row: DocumentItem) {
  reprocessing.value = row.id
  try {
    await documentsApi.reprocess(row.id)
    ElMessage.success(`「${row.filename}」已重新入队处理`)
    await load()
  } catch (e) {
    const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail
    ElMessage.error(detail ?? '重新解析失败')
  } finally {
    reprocessing.value = null
  }
}

function fmtTime(iso: string) {
  return iso.replace('T', ' ').slice(0, 19)
}

async function loadKb() {
  try {
    const kb = await kbApi.detail(kbId)
    kbName.value = kb.name
    myPerm.value = kb.my_perm ?? null
  } catch (e) {
    // 不可见/不存在:统一回列表页,不在此页滞留
    const status = (e as { response?: { status?: number } })?.response?.status
    if (status === 404) {
      ElMessage.error('知识库不存在或无权访问')
      router.replace({ name: 'kb' })
      return
    }
    /* 名称加载失败不阻塞页面 */
  }
}

onMounted(() => {
  if (!Number.isInteger(kbId) || kbId <= 0) {
    ElMessage.error('无效的知识库')
    router.replace({ name: 'kb' })
    return
  }
  loadKb()
  load()
})

onUnmounted(() => {
  disposed = true
  if (timer !== undefined) window.clearInterval(timer)
})
</script>

<template>
  <div class="docs-page">
    <PageHeader :title="kbName" :description="`共 ${docs.length} 篇 · ${processingCount} 个处理中`">
      <template #actions>
        <el-select v-model="statusFilter" placeholder="全部状态" clearable class="status-filter">
          <el-option
            v-for="(meta, key) in STATUS_META"
            :key="key"
            :label="meta.label"
            :value="key"
          />
        </el-select>
        <el-input
          v-model="keyword"
          :prefix-icon="Search"
          placeholder="搜索文件名"
          clearable
          class="doc-search"
        />
        <el-button :icon="ArrowLeft" @click="router.push({ name: 'kb' })">返回</el-button>
      </template>
    </PageHeader>

    <el-card v-if="canEdit" class="upload-card" shadow="never">
      <el-upload
        drag
        multiple
        accept=".pdf,.docx,.xlsx,.jpg,.jpeg,.png"
        :show-file-list="false"
        :disabled="uploading"
        :before-upload="beforeUpload"
        :http-request="doUpload"
      >
        <div class="el-upload__text">将文件拖到此处,或点击上传</div>
        <template #tip>
          <div class="el-upload__tip">支持 .pdf / .docx / .xlsx / .jpg / .png(扫描件自动 OCR),单个文件不超过 20MB</div>
        </template>
      </el-upload>
      <el-progress
        v-if="uploading"
        :percentage="uploadPercent"
        :stroke-width="6"
        class="upload-progress"
      />
    </el-card>

    <el-table v-loading="loading" :data="pagedDocs" class="docs-table">
      <template #empty>
        <el-empty description="暂无文档,上传一个试试" />
      </template>
      <el-table-column prop="filename" label="文件名" min-width="220" show-overflow-tooltip />
      <el-table-column label="状态" width="120" align="center">
        <template #default="{ row }">
          <span class="status-cell">
            <el-tooltip
              :disabled="row.status !== 'failed' || !row.error_msg"
              :content="row.error_msg ?? ''"
              placement="top"
            >
              <el-tag :type="statusMeta(row.status).type">{{ statusMeta(row.status).label }}</el-tag>
            </el-tooltip>
            <el-tag v-if="row.ocr_used" type="success" size="small">OCR</el-tag>
          </span>
        </template>
      </el-table-column>
      <el-table-column prop="chunk_count" label="分块数" width="90" align="center" />
      <el-table-column label="上传时间" width="180">
        <template #default="{ row }">{{ fmtTime(row.created_at) }}</template>
      </el-table-column>
      <el-table-column label="操作" width="170" align="center">
        <template #default="{ row }">
          <el-button link type="primary" @click="openChunks(row)">查看分块</el-button>
          <el-button
            v-if="canEdit && REPROCESSABLE.includes(row.status)"
            link
            type="warning"
            :loading="reprocessing === row.id"
            @click="reprocess(row)"
          >
            重新解析
          </el-button>
        </template>
      </el-table-column>
    </el-table>

    <el-pagination
      v-if="filteredDocs.length > page.pageSize"
      v-model:current-page="page.page"
      :page-size="page.pageSize"
      class="docs-pagination"
      layout="total, prev, pager, next"
      :total="filteredDocs.length"
    />

    <el-drawer v-model="drawerVisible" title="文档分块" size="50%">
      <div v-if="drawerDoc" class="drawer-head">
        <span>{{ drawerDoc.filename }}</span>
        <el-tag :type="statusMeta(drawerDoc.status).type" size="small">
          {{ statusMeta(drawerDoc.status).label }}
        </el-tag>
        <span class="drawer-meta">共 {{ chunks.total }} 块</span>
      </div>
      <el-table v-loading="chunksLoading" :data="chunks.items" size="small">
        <el-table-column prop="chunk_index" label="#" width="60" align="center" />
        <el-table-column label="页码" width="70" align="center">
          <template #default="{ row }">{{ row.page_no ?? '—' }}</template>
        </el-table-column>
        <el-table-column prop="char_len" label="字符数" width="80" align="center" />
        <el-table-column prop="content_preview" label="内容预览" min-width="280" show-overflow-tooltip />
      </el-table>
      <el-pagination
        v-model:current-page="chunkPage.page"
        v-model:page-size="chunkPage.pageSize"
        class="drawer-pagination"
        layout="total, prev, pager, next, sizes"
        :page-sizes="[10, 20, 50]"
        :total="chunks.total"
        @current-change="fetchChunks"
        @size-change="fetchChunks"
      />
    </el-drawer>
  </div>
</template>

<style scoped>
.doc-search {
  width: 200px;
}
.status-filter {
  width: 130px;
}
.docs-pagination {
  margin-top: var(--app-spacing-md);
  justify-content: flex-end;
}
.upload-card {
  margin-bottom: 16px;
}
.upload-card :deep(.el-upload-dragger) {
  padding: 20px 0;
}
.upload-progress {
  margin-top: 12px;
}
.docs-table {
  width: 100%;
}
.status-cell {
  display: inline-flex;
  gap: 4px;
  align-items: center;
}
.drawer-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 12px;
  font-weight: 600;
}
.drawer-meta {
  font-weight: 400;
  color: var(--el-text-color-secondary);
}
.drawer-pagination {
  margin-top: 12px;
  justify-content: flex-end;
}
</style>
