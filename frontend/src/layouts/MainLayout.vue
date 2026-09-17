<script setup lang="ts">
import { computed, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  ChatDotRound,
  Collection,
  Document,
  HomeFilled,
  Moon,
  Sunny,
  User,
} from '@element-plus/icons-vue'
import { useAuthStore } from '@/stores/auth'
import { useTheme } from '@/composables/useTheme'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const { isDark, toggle } = useTheme()

// kb 子路由(/kb/3/docs)时菜单仍高亮"知识库"
const activeIndex = computed(() => (route.path.startsWith('/kb') ? '/kb' : route.path))
const pageTitle = computed(() => (route.meta.title as string) ?? '')
const roleLabel = computed(() =>
  auth.user?.role === 'admin' ? '管理员' : auth.user?.role === 'editor' ? '编辑' : '只读',
)

onMounted(() => {
  // 头部显示当前用户名:M1 的 fetchUser 死代码至此激活。
  // 401 由 http 响应拦截器统一清 token 跳登录;其余失败不阻塞布局。
  if (auth.isLoggedIn && !auth.user) {
    auth.fetchUser().catch(() => {})
  }
})

function onLogout() {
  auth.logout()
  router.push({ name: 'login' })
}
</script>

<template>
  <el-container class="app-shell">
    <el-aside width="220px" class="app-aside">
      <div class="brand">
        <span class="brand-mark">AI</span>
        <span class="brand-name">AIRag 知识库</span>
      </div>
      <el-menu :default-active="activeIndex" router class="app-menu">
        <el-menu-item index="/">
          <el-icon><HomeFilled /></el-icon><span>首页</span>
        </el-menu-item>
        <el-menu-item index="/kb">
          <el-icon><Collection /></el-icon><span>知识库</span>
        </el-menu-item>
        <el-menu-item index="/chat">
          <el-icon><ChatDotRound /></el-icon><span>对话</span>
        </el-menu-item>
        <el-menu-item-group v-if="auth.user?.role === 'admin'" title="管理">
          <el-menu-item index="/admin/users">
            <el-icon><User /></el-icon><span>用户管理</span>
          </el-menu-item>
          <el-menu-item index="/admin/audit-logs">
            <el-icon><Document /></el-icon><span>审计日志</span>
          </el-menu-item>
        </el-menu-item-group>
      </el-menu>
    </el-aside>
    <el-container>
      <el-header class="app-header">
        <span class="page-title">{{ pageTitle }}</span>
        <div class="header-actions">
          <el-button :icon="isDark ? Sunny : Moon" circle @click="toggle" />
          <el-dropdown v-if="auth.user">
            <span class="user-chip">
              <span class="avatar">{{ auth.user.username.slice(0, 1).toUpperCase() }}</span>
              <span class="username">{{ auth.user.username }}</span>
              <el-tag size="small" type="info">{{ roleLabel }}</el-tag>
            </span>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item @click="onLogout">退出登录</el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </div>
      </el-header>
      <el-main class="app-main">
        <router-view />
      </el-main>
    </el-container>
  </el-container>
</template>

<style scoped>
.app-shell {
  height: 100vh;
}
.app-aside {
  border-right: 1px solid var(--app-card-border);
  background: var(--app-card-bg);
}
.brand {
  display: flex;
  align-items: center;
  gap: var(--app-spacing-sm);
  padding: var(--app-spacing-lg);
}
.brand-mark {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 32px;
  height: 32px;
  border-radius: var(--app-radius-sm);
  background: var(--el-color-primary);
  color: #fff;
  font-size: 14px;
  font-weight: 700;
}
.brand-name {
  font-weight: 600;
  font-size: 15px;
}
.app-menu {
  border-right: none;
}
.app-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  border-bottom: 1px solid var(--app-card-border);
  background: var(--app-card-bg);
}
.page-title {
  font-size: 15px;
  font-weight: 600;
}
.header-actions {
  display: flex;
  align-items: center;
  gap: var(--app-spacing-md);
}
.user-chip {
  display: inline-flex;
  align-items: center;
  gap: var(--app-spacing-sm);
  cursor: pointer;
  outline: none;
}
.avatar {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 28px;
  height: 28px;
  border-radius: 50%;
  background: var(--el-color-primary-light-8);
  color: var(--el-color-primary);
  font-size: 13px;
  font-weight: 600;
}
.username {
  font-size: 14px;
  color: var(--el-text-color-regular);
}
.app-main {
  background: var(--app-bg);
}
</style>
