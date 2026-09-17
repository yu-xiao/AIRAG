<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox, type FormInstance, type FormRules } from 'element-plus'
import PageHeader from '@/components/PageHeader.vue'
import { keysApi, type ApiKeyCreated, type ApiKeyItem } from '@/api/keys'

const loading = ref(false)
const items = ref<ApiKeyItem[]>([])

const dialogVisible = ref(false)
const submitting = ref(false)
const formRef = ref<FormInstance>()
const form = reactive({ name: '', expires: 'permanent' as 'permanent' | '7' | '30' | '90' })
const rules: FormRules = {
  name: [
    { required: true, message: '请输入密钥名称', trigger: 'blur' },
    { max: 64, message: '最长 64 字符', trigger: 'blur' },
  ],
}

const created = ref<ApiKeyCreated | null>(null)

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
  await formEl.validate()
  submitting.value = true
  try {
    created.value = await keysApi.create({
      name: form.name,
      expires_in_days:
        form.expires === 'permanent' ? null : Number(form.expires),
    })
    dialogVisible.value = false
    form.name = ''
    form.expires = 'permanent'
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
    <PageHeader title="API 密钥" description="供外部 Agent(MCP / REST)访问知识库的凭证,权限与你当前账号一致。">
      <template #actions>
        <el-button type="primary" @click="dialogVisible = true">创建密钥</el-button>
      </template>
    </PageHeader>

    <el-card shadow="never" class="card">
      <el-table v-loading="loading" :data="items">
        <el-table-column prop="name" label="名称" min-width="140" />
        <el-table-column prop="key_prefix" label="前缀" min-width="140" />
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
</style>
