<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, type FormInstance, type FormRules } from 'element-plus'
import { kbApi, type KbItem, type KbMember } from '@/api/kb'
import { useAuthStore } from '@/stores/auth'

const router = useRouter()
const auth = useAuthStore()
const loading = ref(false)
const list = ref<KbItem[]>([])

const isViewer = () => auth.user?.role === 'viewer'

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

onMounted(load)
</script>

<template>
  <div class="kb-page">
    <div class="page-header">
      <h2>知识库</h2>
      <el-button v-if="!isViewer()" type="primary" @click="openCreate">新建知识库</el-button>
    </div>

    <el-table v-loading="loading" :data="list" class="kb-table">
      <template #empty>
        <el-empty description="暂无知识库,点击右上角新建" />
      </template>
      <el-table-column label="名称" min-width="180">
        <template #default="{ row }">
          <el-link type="primary" @click="openDocs(row)">{{ row.name }}</el-link>
        </template>
      </el-table-column>
      <el-table-column prop="description" label="描述" min-width="220">
        <template #default="{ row }">{{ row.description ?? '—' }}</template>
      </el-table-column>
      <el-table-column label="我的权限" width="110" align="center">
        <template #default="{ row }">
          <el-tag :type="permMeta(row.my_perm).type" size="small">
            {{ permMeta(row.my_perm).label }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="doc_count" label="文档数" width="90" align="center" />
      <el-table-column label="创建时间" width="170">
        <template #default="{ row }">{{ fmtTime(row.created_at) }}</template>
      </el-table-column>
      <el-table-column label="操作" width="170" align="center">
        <template #default="{ row }">
          <el-button link type="primary" @click="openDocs(row)">进入</el-button>
          <el-button v-if="row.my_perm === 'owner'" link type="primary" @click="openMembers(row)">
            成员
          </el-button>
        </template>
      </el-table-column>
    </el-table>

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
.page-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 16px;
}
.page-header h2 {
  margin: 0;
}
.kb-table {
  width: 100%;
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
