<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { adminApi, type AdminUser } from '@/api/admin'
import PageHeader from '@/components/PageHeader.vue'

const loading = ref(false)
const users = ref<AdminUser[]>([])

const ROLE_LABEL: Record<AdminUser['role'], string> = {
  admin: '管理员',
  editor: '编辑',
  viewer: '只读',
}

async function load() {
  loading.value = true
  try {
    users.value = await adminApi.listUsers()
  } catch {
    ElMessage.error('加载用户列表失败')
  } finally {
    loading.value = false
  }
}

async function changeRole(row: AdminUser, role: string) {
  try {
    await adminApi.updateUser(row.id, { role: role as AdminUser['role'] })
    ElMessage.success(`「${row.username}」角色已更新`)
  } catch (e) {
    const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail
    ElMessage.error(detail ?? '更新角色失败')
  } finally {
    await load()
  }
}

async function toggleActive(row: AdminUser, active: boolean) {
  try {
    await adminApi.updateUser(row.id, { is_active: active })
    ElMessage.success(`「${row.username}」${active ? '已启用' : '已禁用'}`)
  } catch (e) {
    const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail
    ElMessage.error(detail ?? '更新状态失败')
  } finally {
    await load()
  }
}

onMounted(load)
</script>

<template>
  <div class="users-page">
    <PageHeader title="用户管理" description="管理系统用户角色与启用状态" />

    <el-table v-loading="loading" :data="users" class="users-table">
      <el-table-column prop="id" label="ID" width="70" align="center" />
      <el-table-column prop="username" label="用户名" min-width="160" />
      <el-table-column label="角色" width="160">
        <template #default="{ row }">
          <el-select :model-value="row.role" size="small" @change="(v: string) => changeRole(row, v)">
            <el-option :label="`管理员(${ROLE_LABEL.admin})`" value="admin" />
            <el-option :label="`编辑(${ROLE_LABEL.editor})`" value="editor" />
            <el-option :label="`只读(${ROLE_LABEL.viewer})`" value="viewer" />
          </el-select>
        </template>
      </el-table-column>
      <el-table-column label="状态" width="120" align="center">
        <template #default="{ row }">
          <el-switch
            :model-value="row.is_active"
            @change="(v: string | number | boolean) => toggleActive(row, Boolean(v))"
          />
        </template>
      </el-table-column>
    </el-table>
  </div>
</template>

<style scoped>
.users-table {
  width: 100%;
  max-width: 720px;
  border-radius: var(--app-radius);
}
</style>
