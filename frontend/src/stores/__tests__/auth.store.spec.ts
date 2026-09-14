import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { useAuthStore } from '@/stores/auth'

vi.mock('@/api/auth', () => ({
  authApi: {
    login: vi.fn().mockResolvedValue({ access_token: 'jwt-token', token_type: 'bearer' }),
    register: vi.fn().mockResolvedValue({ id: 1, username: 'alice', role: 'admin', is_active: true }),
    me: vi.fn(),
  },
}))

describe('auth store', () => {
  beforeEach(() => {
    localStorage.clear()
    setActivePinia(createPinia())
  })

  it('login stores token in state and localStorage', async () => {
    const store = useAuthStore()
    await store.login('alice', 'secret123')
    expect(store.token).toBe('jwt-token')
    expect(localStorage.getItem('airag_token')).toBe('jwt-token')
    expect(store.isLoggedIn).toBe(true)
  })

  it('logout clears token', async () => {
    const store = useAuthStore()
    await store.login('alice', 'secret123')
    store.logout()
    expect(store.token).toBeNull()
    expect(localStorage.getItem('airag_token')).toBeNull()
    expect(store.isLoggedIn).toBe(false)
  })
})
