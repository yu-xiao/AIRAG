import { defineStore } from 'pinia'
import { authApi, type UserResponse } from '@/api/auth'

const TOKEN_KEY = 'airag_token'

export const useAuthStore = defineStore('auth', {
  state: () => ({
    token: localStorage.getItem(TOKEN_KEY),
    user: null as UserResponse | null,
  }),
  getters: {
    isLoggedIn: (state) => !!state.token,
  },
  actions: {
    async login(username: string, password: string) {
      const { access_token } = await authApi.login({ username, password })
      this.token = access_token
      localStorage.setItem(TOKEN_KEY, access_token)
    },
    async fetchUser() {
      this.user = await authApi.me()
    },
    clear() {
      this.token = null
      this.user = null
      localStorage.removeItem(TOKEN_KEY)
    },
    logout() {
      this.clear()
    },
  },
})
