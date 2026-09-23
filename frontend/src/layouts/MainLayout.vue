<script setup lang="ts">
import { computed, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  ChatDotRound,
  Collection,
  Document,
  HomeFilled,
  Key,
  Moon,
  Promotion,
  Sunny,
  TrendCharts,
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
        <el-menu-item index="/eval">
          <el-icon><TrendCharts /></el-icon><span>评估</span>
        </el-menu-item>
        <el-menu-item index="/keys">
          <el-icon><Key /></el-icon><span>API 密钥</span>
        </el-menu-item>
        <el-menu-item-group v-if="auth.user?.role === 'admin'" title="管理">
          <el-menu-item index="/admin/users">
            <el-icon><User /></el-icon><span>用户管理</span>
          </el-menu-item>
          <el-menu-item index="/admin/audit-logs">
            <el-icon><Document /></el-icon><span>审计日志</span>
          </el-menu-item>
          <el-menu-item index="/admin/webhooks">
            <el-icon><Promotion /></el-icon><span>出站推送</span>
          </el-menu-item>
        </el-menu-item-group>
      </el-menu>
    </el-aside>
    <el-container>
      <el-header class="app-header" height="56px">
        <span class="page-title">{{ pageTitle }}</span>
        <div class="header-actions">
          <el-button
            :icon="isDark ? Sunny : Moon"
            circle
            aria-label="切换主题"
            class="theme-toggle"
            @click="toggle"
          />
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
  width: 34px;
  height: 34px;
  border-radius: 10px;
  background: var(--app-brand-grad);
  color: #fff;
  font-size: 14px;
  font-weight: 700;
  box-shadow: var(--app-shadow-brand);
}
.brand-name {
  font-weight: 650;
  font-size: 15px;
  letter-spacing: 0.2px;
}
.app-menu {
  border-right: none;
  padding: 4px 8px;
}
.app-menu :deep(.el-menu-item) {
  height: 42px;
  line-height: 42px;
  margin: 2px 0;
  border-radius: var(--app-radius-sm);
  transition:
    background-color 0.2s ease,
    color 0.2s ease;
}
.app-menu :deep(.el-menu-item:hover) {
  background: var(--el-fill-color-light);
}
.app-menu :deep(.el-menu-item.is-active) {
  background: var(--el-color-primary-light-9);
}
.app-menu :deep(.el-menu-item-group__title) {
  padding-left: 12px;
}
.app-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  border-bottom: 1px solid var(--app-card-border);
  background: var(--app-card-bg);
}
.page-title {
  font-size: 16px;
  font-weight: 650;
}
.header-actions {
  display: flex;
  align-items: center;
  gap: var(--app-spacing-md);
}
.theme-toggle :deep(.el-icon) {
  transition: transform 0.3s ease;
}
.theme-toggle:hover :deep(.el-icon) {
  transform: rotate(30deg);
}
.user-chip {
  display: inline-flex;
  align-items: center;
  gap: var(--app-spacing-sm);
  padding: 4px 12px 4px 5px;
  border: 1px solid var(--app-card-border);
  border-radius: 999px;
  background: var(--app-card-bg);
  cursor: pointer;
  outline: none;
  transition:
    box-shadow 0.2s ease,
    border-color 0.2s ease;
}
.user-chip:hover {
  border-color: var(--el-color-primary-light-5);
  box-shadow: var(--app-shadow-card);
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
@media (prefers-reduced-motion: reduce) {
  .theme-toggle :deep(.el-icon) {
    transition: none;
  }
  .app-menu :deep(.el-menu-item),
  .user-chip {
    transition: none;
  }
}
</style>
