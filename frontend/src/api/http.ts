import axios from 'axios'
import router from '@/router'
import { useAuthStore } from '@/stores/auth'

const http = axios.create({ baseURL: '/api' })

http.interceptors.request.use((config) => {
  const auth = useAuthStore()
  if (auth.token) {
    config.headers.Authorization = `Bearer ${auth.token}`
  }
  return config
})

http.interceptors.response.use(
  (res) => res,
  (err) => {
    if (err.response?.status === 401) {
      useAuthStore().clear()
      router.push({ name: 'login' })
    }
    return Promise.reject(err)
  },
)

export default http
