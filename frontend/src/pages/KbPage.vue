<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, type FormInstance, type FormRules } from 'element-plus'
import { Search } from '@element-plus/icons-vue'
import { kbApi, type KbItem, type KbMember } from '@/api/kb'
import { useAuthStore } from '@/stores/auth'
import PageHeader from '@/components/PageHeader.vue'

const router = useRouter()
const route = useRoute()
const auth = useAuthStore()
const loading = ref(false)
const list = ref<KbItem[]>([])

const isViewer = () => auth.user?.role === 'viewer'

const keyword = ref('')
const filteredList = computed(() => {
  const k = keyword.value.trim().toLowerCase()
  if (!k) return list.value
  return list.value.filter(
    (kb) =>
      kb.name.toLowerCase().includes(k) ||
      (kb.description ?? '').toLowerCase().includes(k),
  )
})
const totalDocs = computed(() => list.value.reduce((s, kb) => s + (kb.doc_count ?? 0), 0))

const PERM_META: Record<string, { label: string; type: 'danger' | 'warning' | 'info' }> = {
  owner: { label: '库主', type: 'danger' },
  editor: { label: '可编辑', type: 'warning' },
  viewer: { label: '只读', type: 'info' },
}

function permMeta(perm?: string | null) {
  return PERM_META[perm ?? ''] ?? { label: '—', type: 'info' as const }
}

const dialogVisible = ref(false)
const submitting = ref(false)
const formRef = ref<FormInstance>()
const form = reactive({ name: '', description: '' })

const rules: FormRules = {
  name: [
    { required: true, message: '请输入知识库名称', trigger: 'blur' },
    { max: 128, message: '名称最多 128 字符', trigger: 'blur' },
  ],
  description: [{ max: 512, message: '描述最多 512 字符', trigger: 'blur' }],
}

async function load() {
  loading.value = true
  try {
    list.value = await kbApi.list()
  } catch {
    ElMessage.error('加载知识库列表失败')
  } finally {
    loading.value = false
  }
}

function openCreate() {
  form.name = ''
  form.description = ''
  formRef.value?.resetFields()
  dialogVisible.value = true
}

async function submit() {
  const valid = await formRef.value?.validate().catch(() => false)
  if (!valid) return
  submitting.value = true
  try {
    await kbApi.create({
      name: form.name.trim(),
      description: form.description.trim() || null,
    })
    ElMessage.success('知识库创建成功')
    dialogVisible.value = false
    await load()
  } catch (e) {
    const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail
    ElMessage.error(detail ?? '创建失败')
  } finally {
    submitting.value = false
  }
}

function openDocs(row: KbItem) {
  router.push(`/kb/${row.id}/docs`)
}

// ---- 成员管理(owner 可见)----
const memberVisible = ref(false)
const memberKb = ref<KbItem | null>(null)
const members = ref<KbMember[]>([])
const membersLoading = ref(false)
const grantForm = reactive({ username: '', perm: 'viewer' as 'viewer' | 'editor' })
const granting = ref(false)

async function loadMembers() {
  if (!memberKb.value) return
  membersLoading.value = true
  try {
    members.value = await kbApi.members(memberKb.value.id)
  } catch {
    ElMessage.error('加载成员列表失败')
  } finally {
    membersLoading.value = false
  }
}

function openMembers(row: KbItem) {
  memberKb.value = row
  grantForm.username = ''
  grantForm.perm = 'viewer'
  memberVisible.value = true
  loadMembers()
}

async function submitGrant() {
  if (!memberKb.value) return
  const username = grantForm.username.trim()
  if (!username) {
    ElMessage.warning('请输入用户名')
    return
  }
  granting.value = true
  try {
    await kbApi.grant(memberKb.value.id, { username, perm: grantForm.perm })
    ElMessage.success(`已授权「${username}」`)
    grantForm.username = ''
    await loadMembers()
  } catch (e) {
    const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail
    ElMessage.error(detail ?? '授权失败')
  } finally {
    granting.value = false
  }
}

async function changePerm(row: KbMember, perm: 'viewer' | 'editor') {
  if (!memberKb.value) return
  try {
    await kbApi.grant(memberKb.value.id, { username: row.username, perm })
    ElMessage.success(`「${row.username}」权限已更新`)
  } catch {
    ElMessage.error('更新权限失败')
  } finally {
    await loadMembers()
  }
}

async function removeMember(row: KbMember) {
  if (!memberKb.value) return
  try {
    await kbApi.revoke(memberKb.value.id, row.username)
    ElMessage.success(`已移除「${row.username}」`)
    await loadMembers()
  } catch {
    ElMessage.error('移除失败')
  }
}

function fmtTime(iso: string) {
  return iso.replace('T', ' ').slice(0, 19)
}

onMounted(() => {
  load()
  // 工作台快捷入口:自动打开新建对话框(viewer 无权创建,跳过)
  if (route.query.create === '1' && !isViewer()) {
    openCreate()
  }
})
</script>

<template>
  <div class="kb-page">
    <PageHeader title="知识库" :description="`共 ${list.length} 个库 · ${totalDocs} 篇文档`">
      <template #actions>
        <el-input
          v-model="keyword"
          :prefix-icon="Search"
          placeholder="搜索名称/描述"
          clearable
          class="kb-search"
        />
        <el-button v-if="!isViewer()" type="primary" @click="openCreate">新建知识库</el-button>
      </template>
    </PageHeader>

    <div v-loading="loading" class="kb-grid">
      <el-empty v-if="filteredList.length === 0" description="暂无知识库,点击右上角新建" class="kb-empty" />
      <el-card v-for="row in filteredList" :key="row.id" shadow="never" class="kb-card" @click="openDocs(row)">
        <div class="kb-card-head">
          <span class="kb-card-name">{{ row.name }}</span>
          <el-tag :type="permMeta(row.my_perm).type" size="small">
            {{ permMeta(row.my_perm).label }}
          </el-tag>
        </div>
        <p class="kb-card-desc">{{ row.description ?? '暂无描述' }}</p>
        <div class="kb-card-meta">
          <span>{{ row.doc_count }} 篇文档</span>
          <span>{{ fmtTime(row.created_at) }}</span>
        </div>
        <div class="kb-card-actions" @click.stop>
          <el-button size="small" type="primary" plain @click="openDocs(row)">进入</el-button>
          <el-button v-if="row.my_perm === 'owner'" size="small" plain @click="openMembers(row)">
            成员
          </el-button>
        </div>
      </el-card>
    </div>

    <el-dialog v-model="dialogVisible" title="新建知识库" width="480px">
      <el-form ref="formRef" :model="form" :rules="rules" label-position="top">
        <el-form-item label="名称" prop="name">
          <el-input v-model="form.name" placeholder="请输入知识库名称" maxlength="128" />
        </el-form-item>
        <el-form-item label="描述" prop="description">
          <el-input
            v-model="form.description"
            type="textarea"
            :rows="3"
            placeholder="可选,不超过 512 字符"
            maxlength="512"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="submitting" @click="submit">创建</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="memberVisible" :title="`成员管理 — ${memberKb?.name ?? ''}`" width="560px">
      <el-table v-loading="membersLoading" :data="members" size="small">
        <template #empty>
          <el-empty description="尚无成员,按用户名添加" :image-size="48" />
        </template>
        <el-table-column prop="username" label="用户名" min-width="140" />
        <el-table-column label="权限" width="150">
          <template #default="{ row }">
            <el-select
              :model-value="row.perm"
              size="small"
              @change="(v: string) => changePerm(row, v as 'viewer' | 'editor')"
            >
              <el-option label="只读(viewer)" value="viewer" />
              <el-option label="可编辑(editor)" value="editor" />
            </el-select>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="80" align="center">
          <template #default="{ row }">
            <el-button link type="danger" @click="removeMember(row)">移除</el-button>
          </template>
        </el-table-column>
      </el-table>
      <div class="grant-row">
        <el-input
          v-model="grantForm.username"
          placeholder="用户名"
          class="grant-input"
          maxlength="32"
        />
        <el-select v-model="grantForm.perm" class="grant-perm">
          <el-option label="只读(viewer)" value="viewer" />
          <el-option label="可编辑(editor)" value="editor" />
        </el-select>
        <el-button type="primary" :loading="granting" @click="submitGrant">添加</el-button>
      </div>
    </el-dialog>
  </div>
</template>

<style scoped>
.kb-search {
  width: 220px;
}
.kb-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: var(--app-spacing-md);
  min-height: 120px;
}
.kb-empty {
  grid-column: 1 / -1;
}
.kb-card {
  cursor: pointer;
  border-radius: var(--app-radius);
  transition: box-shadow 0.2s;
}
.kb-card:hover {
  box-shadow: var(--app-shadow-card);
}
.kb-card-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: var(--app-spacing-sm);
}
.kb-card-name {
  font-weight: 600;
  font-size: 15px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.kb-card-desc {
  margin: var(--app-spacing-sm) 0;
  font-size: 13px;
  color: var(--el-text-color-secondary);
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
  min-height: 40px;
}
.kb-card-meta {
  display: flex;
  justify-content: space-between;
  font-size: 12px;
  color: var(--el-text-color-secondary);
  margin-bottom: var(--app-spacing-md);
}
.kb-card-actions {
  display: flex;
  gap: var(--app-spacing-sm);
}
.grant-row {
  display: flex;
  gap: 8px;
  margin-top: 12px;
}
.grant-input {
  flex: 1;
}
.grant-perm {
  width: 150px;
}
</style>
