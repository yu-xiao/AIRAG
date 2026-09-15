<script setup lang="ts">
import { onMounted, onUnmounted, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, type UploadRawFile, type UploadRequestOptions } from 'element-plus'
import { ArrowLeft } from '@element-plus/icons-vue'
import { documentsApi, type ChunksResponse, type DocumentItem } from '@/api/documents'
import { kbApi } from '@/api/kb'

const route = useRoute()
const router = useRouter()

const kbId = Number(route.params.id)
const kbName = ref(`知识库 #${kbId}`)

const loading = ref(false)
const docs = ref<DocumentItem[]>([])

const MAX_UPLOAD_MB = 20
const ALLOWED_EXTS = ['.pdf', '.docx', '.xlsx']
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

// ---- 上传(手动模式:http-request 调 documentsApi.upload)----
const uploading = ref(false)
const uploadPercent = ref(0)

function beforeUpload(raw: UploadRawFile) {
  const ext = raw.name.slice(raw.name.lastIndexOf('.')).toLowerCase()
  if (!ALLOWED_EXTS.includes(ext)) {
    ElMessage.error(`不支持的文件类型 ${ext},仅支持 .pdf/.docx/.xlsx`)
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

function hasActive() {
  return docs.value.some((d) => NON_TERMINAL.includes(d.status))
}

function syncPolling() {
  if (hasActive()) {
    if (timer === undefined) timer = window.setInterval(load, 3000)
  } else if (timer !== undefined) {
    window.clearInterval(timer)
    timer = undefined
  }
}

async function load() {
  loading.value = true
  try {
    docs.value = await documentsApi.list(kbId)
  } catch {
    ElMessage.error('加载文档列表失败')
  } finally {
    loading.value = false
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

function fmtTime(iso: string) {
  return iso.replace('T', ' ').slice(0, 19)
}

async function loadKbName() {
  try {
    const kb = (await kbApi.list()).find((k) => k.id === kbId)
    if (kb) kbName.value = kb.name
  } catch {
    /* 名称加载失败不阻塞页面 */
  }
}

onMounted(() => {
  if (!Number.isInteger(kbId) || kbId <= 0) {
    ElMessage.error('无效的知识库')
    router.replace({ name: 'kb' })
    return
  }
  loadKbName()
  load()
})

onUnmounted(() => {
  if (timer !== undefined) window.clearInterval(timer)
})
</script>

<template>
  <div class="docs-page">
    <div class="page-header">
      <div class="page-title">
        <el-button :icon="ArrowLeft" link @click="router.push({ name: 'kb' })">返回知识库</el-button>
        <h2>{{ kbName }}</h2>
      </div>
    </div>

    <el-card class="upload-card" shadow="never">
      <el-upload
        drag
        multiple
        accept=".pdf,.docx,.xlsx"
        :show-file-list="false"
        :disabled="uploading"
        :before-upload="beforeUpload"
        :http-request="doUpload"
      >
        <div class="el-upload__text">将文件拖到此处,或点击上传</div>
        <template #tip>
          <div class="el-upload__tip">支持 .pdf / .docx / .xlsx,单个文件不超过 20MB</div>
        </template>
      </el-upload>
      <el-progress
        v-if="uploading"
        :percentage="uploadPercent"
        :stroke-width="6"
        class="upload-progress"
      />
    </el-card>

    <el-table v-loading="loading" :data="docs" class="docs-table">
      <template #empty>
        <el-empty description="暂无文档,上传一个试试" />
      </template>
      <el-table-column prop="filename" label="文件名" min-width="220" show-overflow-tooltip />
      <el-table-column label="状态" width="120" align="center">
        <template #default="{ row }">
          <el-tooltip
            :disabled="row.status !== 'failed' || !row.error_msg"
            :content="row.error_msg ?? ''"
            placement="top"
          >
            <el-tag :type="statusMeta(row.status).type">{{ statusMeta(row.status).label }}</el-tag>
          </el-tooltip>
        </template>
      </el-table-column>
      <el-table-column prop="chunk_count" label="分块数" width="90" align="center" />
      <el-table-column label="上传时间" width="180">
        <template #default="{ row }">{{ fmtTime(row.created_at) }}</template>
      </el-table-column>
      <el-table-column label="操作" width="120" align="center">
        <template #default="{ row }">
          <el-button link type="primary" @click="openChunks(row)">查看分块</el-button>
        </template>
      </el-table-column>
    </el-table>

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
.page-header {
  margin-bottom: 16px;
}
.page-title {
  display: flex;
  align-items: center;
  gap: 12px;
}
.page-title h2 {
  margin: 0;
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
