<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox, type FormInstance, type FormRules } from 'element-plus'
import PageHeader from '@/components/PageHeader.vue'
import { keysApi, type ApiKeyCreated, type ApiKeyItem } from '@/api/keys'
import { usersApi, type UserBrief } from '@/api/users'
import { kbApi } from '@/api/kb'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
const isAdmin = computed(() => auth.user?.role === 'admin')

const loading = ref(false)
const items = ref<ApiKeyItem[]>([])

const dialogVisible = ref(false)
const submitting = ref(false)
const formRef = ref<FormInstance>()
const form = reactive({
  name: '',
  expires: 'permanent' as 'permanent' | '7' | '30' | '90',
  userId: null as number | null,
  role: 'read_only' as 'read_only' | 'editor',
  kbScope: [] as number[],  // M12:空 = 不限
})
const rules: FormRules = {
  name: [
    { required: true, message: '请输入密钥名称', trigger: 'blur' },
    { max: 64, message: '最长 64 字符', trigger: 'blur' },
  ],
}

const created = ref<ApiKeyCreated | null>(null)

// M9.1:admin 创建时可指定绑定账号(空 = 默认绑定自己)
const userOptions = ref<UserBrief[]>([])
const userSearching = ref(false)
const boundUsername = ref<string | null>(null)

async function searchBindUsers(q: string) {
  userSearching.value = true
  try {
    const found = await usersApi.search(q.trim(), 0, 20)
    // 保留已选项,避免远程搜索刷新后 el-select label 丢成裸 id
    const sel = userOptions.value.find((u) => u.id === form.userId)
    userOptions.value =
      sel && !found.some((u) => u.id === sel.id) ? [sel, ...found] : found
  } catch {
    ElMessage.error('账号搜索失败')
  } finally {
    userSearching.value = false
  }
}

// M12:可访问范围选项——admin 绑定账号后取目标用户可见库,否则取自己的库
const kbOptions = ref<{ id: number; name: string }[]>([])
const kbOptionsLoading = ref(false)

async function loadKbOptions() {
  kbOptionsLoading.value = true
  try {
    if (isAdmin.value && form.userId != null) {
      kbOptions.value = await keysApi.adminUserKbs(form.userId)
    } else {
      kbOptions.value = (await kbApi.list()).map((k) => ({
        id: k.id, name: k.name,
      }))
    }
  } catch {
    ElMessage.error('知识库列表加载失败')
  } finally {
    kbOptionsLoading.value = false
  }
}

// 绑定账号变化即刷新范围选项。用 watch 而非 el-select @change:
// ElSelect 仅在用户点选时 emit change,外部更新 v-model(如测试合成事件)不触发,
// watch 对两种路径都生效,避免真实交互下 change+watch 双重加载。
// M13 收敛:仅在真正切换到不同账号(新值非空且≠旧值)时清空范围并重载;
// 置空(用户清除绑定 / openCreate、submit 复位)只清选项,不发起请求。
watch(() => form.userId, (nv, ov) => {
  if (nv && nv !== ov) {
    form.kbScope = []
    loadKbOptions()
  } else if (!nv) {
    kbOptions.value = []
  }
})

function openCreate() {
  form.name = ''
  form.expires = 'permanent'
  form.userId = null
  form.role = 'read_only'
  form.kbScope = []
  userOptions.value = []
  kbOptions.value = []
  boundUsername.value = null
  formRef.value?.resetFields()
  dialogVisible.value = true
  loadKbOptions()
}

const errMsg = (e: unknown, fallback: string) =>
  (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? fallback

async function load() {
  loading.value = true
  try {
    items.value = await keysApi.list()
  } catch (e) {
    ElMessage.error(errMsg(e, '密钥列表加载失败'))
  } finally {
    loading.value = false
  }
}

async function submit(formEl: FormInstance | undefined) {
  if (!formEl) return
  const valid = await formEl.validate().catch(() => false)
  if (!valid) return
  submitting.value = true
  try {
    const payload = {
      name: form.name,
      role: form.role,
      expires_in_days:
        form.expires === 'permanent' ? null : Number(form.expires),
      kb_scope: form.kbScope.length ? [...form.kbScope] : null,
    }
    if (isAdmin.value && form.userId != null) {
      created.value = await keysApi.createAdmin({ ...payload, user_id: form.userId })
      boundUsername.value =
        userOptions.value.find((u) => u.id === form.userId)?.username ?? null
    } else {
      created.value = await keysApi.create(payload)
      boundUsername.value = null
    }
    dialogVisible.value = false
    form.name = ''
    form.expires = 'permanent'
    form.userId = null
    form.role = 'read_only'
    form.kbScope = []
    await load()
  } catch (e) {
    ElMessage.error(errMsg(e, '创建失败'))
  } finally {
    submitting.value = false
  }
}

async function revoke(row: ApiKeyItem) {
  try {
    await ElMessageBox.confirm(
      `确定吊销「${row.name}」?使用该密钥的 Agent 将立即失去访问权。`,
      '吊销密钥',
      { type: 'warning', confirmButtonText: '吊销', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  try {
    await keysApi.revoke(row.id)
    ElMessage.success('已吊销')
    await load()
  } catch (e) {
    ElMessage.error(errMsg(e, '吊销失败'))
  }
}

async function copyKey() {
  if (!created.value) return
  await navigator.clipboard.writeText(created.value.key)
  ElMessage.success('已复制到剪贴板')
}

const fmt = (s: string | null) => (s ? new Date(s).toLocaleString() : '—')

onMounted(load)
</script>

<template>
  <div class="page">
    <PageHeader title="API 密钥" description="供外部 Agent(MCP / REST)访问知识库的凭证;编辑型密钥还可上传/删除/重解析文档。">
      <template #actions>
        <el-button type="primary" @click="openCreate">创建密钥</el-button>
      </template>
    </PageHeader>

    <el-card shadow="never" class="card">
      <el-table v-loading="loading" :data="items">
        <el-table-column prop="name" label="名称" min-width="140" />
        <el-table-column prop="key_prefix" label="前缀" min-width="140" />
        <el-table-column label="类型" width="90" align="center">
          <template #default="{ row }">
            <el-tag :type="row.role === 'editor' ? 'warning' : 'info'" size="small">
              {{ row.role === 'editor' ? '编辑' : '只读' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="范围" width="100" align="center">
          <template #default="{ row }">
            <el-tooltip
              :content="row.kb_scope ? `仅库 ${row.kb_scope.join(', ')}` : '全部授权库'"
            >
              <el-tag size="small" :type="row.kb_scope ? 'warning' : 'info'">
                {{ row.kb_scope ? `${row.kb_scope.length} 库` : '全部' }}
              </el-tag>
            </el-tooltip>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="100">
          <template #default="{ row }">
            <el-tag :type="row.is_active ? 'success' : 'info'" size="small">
              {{ row.is_active ? '活跃' : '已吊销' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="过期时间" min-width="160">
          <template #default="{ row }">{{ fmt(row.expires_at) }}</template>
        </el-table-column>
        <el-table-column label="最后使用" min-width="160">
          <template #default="{ row }">{{ fmt(row.last_used_at) }}</template>
        </el-table-column>
        <el-table-column label="创建时间" min-width="160">
          <template #default="{ row }">{{ fmt(row.created_at) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="90">
          <template #default="{ row }">
            <el-button v-if="row.is_active" link type="danger" @click="revoke(row)">
              吊销
            </el-button>
          </template>
        </el-table-column>
        <template #empty>
          <el-empty description="还没有密钥,创建一把给外部 Agent 使用" />
        </template>
      </el-table>
    </el-card>

    <el-dialog v-model="dialogVisible" title="创建 API 密钥" width="480px">
      <el-form ref="formRef" :model="form" :rules="rules" label-position="top">
        <el-form-item label="名称" prop="name">
          <el-input v-model="form.name" maxlength="64" placeholder="请输入密钥名称" />
        </el-form-item>
        <el-form-item v-if="isAdmin" label="绑定账号">
          <el-select
            v-model="form.userId"
            filterable
            remote
            clearable
            :remote-method="searchBindUsers"
            :loading="userSearching"
            placeholder="默认绑定当前账号"
            no-data-text="输入用户名搜索"
          >
            <el-option v-for="u in userOptions" :key="u.id" :label="u.username" :value="u.id" />
          </el-select>
        </el-form-item>
        <el-form-item label="密钥类型">
          <el-radio-group v-model="form.role">
            <el-radio value="read_only">只读(检索/问答)</el-radio>
            <el-radio value="editor">编辑(可维护文档)</el-radio>
          </el-radio-group>
        </el-form-item>
        <el-form-item label="可访问范围">
          <el-select
            v-model="form.kbScope"
            multiple
            collapse-tags
            clearable
            :loading="kbOptionsLoading"
            placeholder="不限(全部授权库)"
          >
            <el-option
              v-for="k in kbOptions"
              :key="k.id"
              :label="`${k.name}(#${k.id})`"
              :value="k.id"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="有效期">
          <el-radio-group v-model="form.expires">
            <el-radio value="7">7 天</el-radio>
            <el-radio value="30">30 天</el-radio>
            <el-radio value="90">90 天</el-radio>
            <el-radio value="permanent">永久</el-radio>
          </el-radio-group>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="submitting" @click="submit(formRef)">
          创建
        </el-button>
      </template>
    </el-dialog>

    <el-dialog :model-value="created !== null" title="密钥已创建" width="520px"
               :close-on-click-modal="false" @close="created = null">
      <el-alert type="warning" :closable="false" show-icon
                title="明文密钥仅展示这一次,关闭后无法再次查看。" class="alert" />
      <div class="key-box">
        <code class="key-value">{{ created?.key }}</code>
        <el-button @click="copyKey">复制</el-button>
      </div>
      <p v-if="boundUsername" class="bound-note">
        该密钥已绑定账号:{{ boundUsername }}(权限与该账号一致,不显示在你的密钥列表)
      </p>
      <template #footer>
        <el-button type="primary" @click="created = null">我已保存</el-button>
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
.card {
  border-radius: var(--app-radius, 12px);
  border: 1px solid var(--app-card-border, #e5e7eb);
  background: var(--app-card-bg, #fff);
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
.bound-note {
  margin-top: 8px;
  font-size: 13px;
  color: var(--el-text-color-secondary);
}
</style>
