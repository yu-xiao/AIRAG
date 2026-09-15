<script setup lang="ts">
import { onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useAuthStore } from '@/stores/auth'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()

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
  <el-container style="height: 100vh">
    <el-aside width="200px">
      <el-menu :default-active="route.path" router>
        <el-menu-item index="/">首页</el-menu-item>
        <el-menu-item index="/kb">知识库</el-menu-item>
        <el-menu-item index="/chat">对话</el-menu-item>
        <el-menu-item v-if="auth.user?.role === 'admin'" index="/admin/users">用户管理</el-menu-item>
      </el-menu>
    </el-aside>
    <el-container>
      <el-header style="display: flex; justify-content: flex-end; align-items: center; gap: 12px">
        <span v-if="auth.user" class="header-username">{{ auth.user.username }}</span>
        <el-button link @click="onLogout">退出登录</el-button>
      </el-header>
      <el-main>
        <router-view />
      </el-main>
    </el-container>
  </el-container>
</template>

<style scoped>
.header-username {
  font-size: 14px;
  color: var(--el-text-color-regular);
}
</style>
