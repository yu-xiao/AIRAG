<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { adminApi, type AuditLogItem } from '@/api/admin'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
const loading = ref(false)
const purging = ref(false)
const logs = ref<AuditLogItem[]>([])
const total = ref(0)
const query = reactive({ username: '', action: '', page: 1, pageSize: 20 })

const ACTION_OPTIONS = [
  'login_success', 'login_fail', 'register', 'kb_create',
  'kb_grant', 'kb_revoke', 'doc_upload', 'doc_reprocess',
  'conv_delete', 'user_admin_update', 'ask', 'audit_purge',
]

const ACTION_LABEL: Record<string, string> = {
  login_success: '登录成功',
  login_fail: '登录失败',
  register: '注册',
  kb_create: '建知识库',
  kb_grant: '授权',
  kb_revoke: '取消授权',
  doc_upload: '上传文档',
  doc_reprocess: '重新解析',
  conv_delete: '删会话',
  user_admin_update: '用户管理变更',
  ask: '提问',
  audit_purge: '清理审计',
}

const ACTION_TYPE: Record<string, 'success' | 'danger' | 'warning' | 'info' | 'primary'> = {
  login_success: 'success',
  login_fail: 'danger',
  ask: 'primary',
}

function label(a: string) {
  return ACTION_LABEL[a] ?? a
}

function tagType(a: string) {
  return ACTION_TYPE[a] ?? 'info'
}

async function load() {
  loading.value = true
  try {
    const resp = await adminApi.listAuditLogs({
      username: query.username || undefined,
      action: query.action || undefined,
      page: query.page,
      page_size: query.pageSize,
    })
    logs.value = resp.items
    total.value = resp.total
  } catch {
    ElMessage.error('加载审计日志失败')
  } finally {
    loading.value = false
  }
}

function search() {
  query.page = 1
  load()
}

async function purgeExpired() {
  try {
    await ElMessageBox.confirm(
      '将删除超过保留期（默认 180 天）的审计日志，删除不可恢复。确定执行？',
      '清理过期日志',
      { type: 'warning', confirmButtonText: '清理', cancelButtonText: '取消' },
    )
  } catch {
    return // 用户取消
  }
  purging.value = true
  try {
    const r = await adminApi.purgeExpiredLogs()
    if (r.deleted > 0) {
      ElMessage.success(`已清理 ${r.deleted} 条过期日志`)
    } else {
      ElMessage.info('没有超过保留期的日志')
    }
    await load()
  } catch {
    ElMessage.error('清理失败')
  } finally {
    purging.value = false
  }
}

function fmtTime(iso: string) {
  return iso.replace('T', ' ').slice(0, 19)
}

onMounted(load)
</script>

<template>
  <div class="audit-page">
    <div class="page-header">
      <h2>审计日志</h2>
      <div class="filters">
        <el-input
          v-model="query.username"
          placeholder="用户名"
          clearable
          class="filter-input"
          @keyup.enter="search"
        />
        <el-select v-model="query.action" placeholder="动作" clearable class="filter-select">
          <el-option v-for="a in ACTION_OPTIONS" :key="a" :label="label(a)" :value="a" />
        </el-select>
        <el-button type="primary" @click="search">查询</el-button>
        <el-button
          v-if="auth.user?.role === 'admin'"
          type="danger"
          plain
          :loading="purging"
          @click="purgeExpired"
        >清理过期日志</el-button>
      </div>
    </div>

    <el-table v-loading="loading" :data="logs" class="audit-table">
      <template #empty>
        <el-empty description="暂无审计记录" />
      </template>
      <el-table-column label="时间" width="170">
        <template #default="{ row }">{{ fmtTime(row.created_at) }}</template>
      </el-table-column>
      <el-table-column prop="username" label="用户" width="130" />
      <el-table-column label="动作" width="130">
        <template #default="{ row }">
          <el-tag :type="tagType(row.action)" size="small">{{ label(row.action) }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="target" label="对象" width="110" />
      <el-table-column prop="detail" label="详情" min-width="260" show-overflow-tooltip />
      <el-table-column prop="ip" label="IP" width="130" />
    </el-table>

    <el-pagination
      v-model:current-page="query.page"
      v-model:page-size="query.pageSize"
      class="audit-pagination"
      layout="total, prev, pager, next, sizes"
      :page-sizes="[20, 50, 100]"
      :total="total"
      @current-change="load"
      @size-change="search"
    />
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
.filters {
  display: flex;
  gap: 8px;
}
.filter-input {
  width: 160px;
}
.filter-select {
  width: 160px;
}
.audit-table {
  width: 100%;
}
.audit-pagination {
  margin-top: 12px;
  justify-content: flex-end;
}
</style>
