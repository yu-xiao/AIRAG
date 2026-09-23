import { createRouter, createWebHistory } from 'vue-router'
import { useAuthStore } from '@/stores/auth'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/login', name: 'login', component: () => import('@/pages/LoginPage.vue') },
    {
      path: '/',
      component: () => import('@/layouts/MainLayout.vue'),
      children: [
        { path: '', name: 'home', component: () => import('@/pages/HomePage.vue'), meta: { title: '首页' } },
        { path: 'kb', name: 'kb', component: () => import('@/pages/KbPage.vue'), meta: { title: '知识库' } },
        {
          path: 'kb/:id/docs',
          name: 'kb-docs',
          component: () => import('@/pages/DocsPage.vue'),
          meta: { title: '知识库文档' },
        },
        { path: 'chat', name: 'chat', component: () => import('@/pages/ChatPage.vue'), meta: { title: '对话' } },
        {
          path: 'eval',
          name: 'eval',
          component: () => import('@/pages/EvalPage.vue'),
          meta: { title: '评估' },
        },
        {
          path: 'keys',
          name: 'keys',
          component: () => import('@/pages/KeysPage.vue'),
          meta: { title: 'API 密钥' },
        },
        {
          path: 'admin/users',
          name: 'admin-users',
          component: () => import('@/pages/UsersPage.vue'),
          meta: { title: '用户管理' },
        },
        {
          path: 'admin/audit-logs',
          name: 'admin-audit-logs',
          component: () => import('@/pages/AuditLogPage.vue'),
          meta: { title: '审计日志' },
        },
        {
          path: 'admin/webhooks',
          name: 'admin-webhooks',
          component: () => import('@/pages/WebhooksPage.vue'),
          meta: { title: '出站推送' },
        },
      ],
    },
  ],
})

router.beforeEach((to) => {
  const auth = useAuthStore()
  if (to.name !== 'login' && !auth.isLoggedIn) {
    return { name: 'login' }
  }
})

router.afterEach((to) => {
  document.title = to.meta.title ? `${to.meta.title} · AIRag` : 'AIRag'
})

export default router
