import { mount } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'
import ElementPlus from 'element-plus'
import MainLayout from '@/layouts/MainLayout.vue'

vi.mock('vue-router', () => ({
  useRoute: () => ({ path: '/eval', meta: { title: '评估' } }),
  useRouter: () => ({ push: vi.fn() }),
}))
vi.mock('@/stores/auth', () => ({
  useAuthStore: () => ({
    user: { id: 1, username: 'ed', role: 'editor' },
    isLoggedIn: true,
    fetchUser: vi.fn(),
    logout: vi.fn(),
  }),
}))

describe('MainLayout sidebar', () => {
  // M15 收官裁定:侧边导航「评估记录」→「评估」,与 /eval 路由 title 及页内双页签一致
  it('labels eval menu item 评估, matching route title', () => {
    const w = mount(MainLayout, {
      global: { plugins: [ElementPlus], stubs: { RouterView: true } },
    })
    const evalItem = w.findAll('.el-menu-item').find((m) => m.text() === '评估')
    expect(evalItem).toBeTruthy()
    expect(w.text()).not.toContain('评估记录')
  })
})
