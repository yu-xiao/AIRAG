<script setup lang="ts">
import { useRouter } from 'vue-router'
import { useAuthStore } from '@/stores/auth'

const router = useRouter()
const auth = useAuthStore()

function onLogout() {
  auth.logout()
  router.push({ name: 'login' })
}
</script>

<template>
  <el-container style="height: 100vh">
    <el-aside width="200px">
      <el-menu default-active="home" router>
        <el-menu-item index="home">首页</el-menu-item>
        <el-menu-item index="kb" disabled>知识库(M3)</el-menu-item>
        <el-menu-item index="chat" disabled>对话(M3)</el-menu-item>
      </el-menu>
    </el-aside>
    <el-container>
      <el-header style="display: flex; justify-content: flex-end; align-items: center">
        <el-button link @click="onLogout">退出登录</el-button>
      </el-header>
      <el-main>
        <router-view />
      </el-main>
    </el-container>
  </el-container>
</template>
