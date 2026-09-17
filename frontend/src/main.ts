import { createApp } from 'vue'
import { createPinia } from 'pinia'
import ElementPlus from 'element-plus'
import 'element-plus/dist/index.css'
import 'element-plus/theme-chalk/dark/css-vars.css'
import '@/styles/tokens.css'

import App from './App.vue'
import router from './router'
import { useTheme } from '@/composables/useTheme'

const app = createApp(App)

app.use(createPinia())
app.use(router)
app.use(ElementPlus)

useTheme().init() // 挂载前恢复主题,避免暗色用户看到亮色闪帧

app.mount('#app')
