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
        { path: '', name: 'home', component: () => import('@/pages/HomePage.vue') },
        { path: 'kb', name: 'kb', component: () => import('@/pages/KbPage.vue') },
        { path: 'kb/:id/docs', name: 'kb-docs', component: () => import('@/pages/DocsPage.vue') },
        { path: 'chat', name: 'chat', component: () => import('@/pages/ChatPage.vue') },
        { path: 'admin/users', name: 'admin-users', component: () => import('@/pages/UsersPage.vue') },
        { path: 'admin/audit-logs', name: 'admin-audit-logs', component: () => import('@/pages/AuditLogPage.vue') },
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

export default router
