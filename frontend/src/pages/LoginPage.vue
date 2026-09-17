<script setup lang="ts">
import { reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, type FormInstance, type FormRules } from 'element-plus'
import { useAuthStore } from '@/stores/auth'
import { authApi } from '@/api/auth'

const router = useRouter()
const auth = useAuthStore()
const formRef = ref<FormInstance>()
const loading = ref(false)
const isRegister = ref(false)
const form = reactive({ username: '', password: '' })

const rules: FormRules = {
  username: [{ required: true, message: '请输入用户名', trigger: 'blur' }],
  password: [
    { required: true, message: '请输入密码', trigger: 'blur' },
    { min: 8, message: '密码至少 8 位', trigger: 'blur' },
  ],
}

async function submit() {
  const valid = await formRef.value?.validate().catch(() => false)
  if (!valid) return
  loading.value = true
  try {
    if (isRegister.value) {
      await authApi.register({ ...form })
    }
    await auth.login(form.username, form.password)
    ElMessage.success(isRegister.value ? '注册并登录成功' : '登录成功')
    router.push({ name: 'home' })
  } catch (e) {
    const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail
    ElMessage.error(detail ?? '操作失败,请检查用户名密码')
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="login-page">
    <el-card class="login-card" shadow="never">
      <div class="login-brand">
        <span class="brand-mark">AI</span>
        <h2>AIRag 知识库</h2>
      </div>
      <el-form ref="formRef" :model="form" :rules="rules" label-position="top">
        <el-form-item label="用户名" prop="username">
          <el-input v-model="form.username" placeholder="用户名" />
        </el-form-item>
        <el-form-item label="密码" prop="password">
          <el-input v-model="form.password" type="password" show-password placeholder="密码" />
        </el-form-item>
        <el-button type="primary" :loading="loading" style="width: 100%" @click="submit">
          {{ isRegister ? '注册并登录' : '登录' }}
        </el-button>
        <el-button link style="width: 100%; margin-top: 8px" @click="isRegister = !isRegister">
          {{ isRegister ? '已有账号?去登录' : '没有账号?注册一个' }}
        </el-button>
      </el-form>
    </el-card>
  </div>
</template>

<style scoped>
.login-page {
  display: flex;
  align-items: center;
  justify-content: center;
  height: 100vh;
  background: var(--app-bg);
}
.login-card {
  width: 360px;
  border-radius: var(--app-radius);
}
.login-brand {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: var(--app-spacing-sm);
  margin-bottom: var(--app-spacing-lg);
}
.brand-mark {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 40px;
  height: 40px;
  border-radius: var(--app-radius);
  background: var(--el-color-primary);
  color: #fff;
  font-weight: 700;
}
h2 {
  margin: 0;
}
</style>
