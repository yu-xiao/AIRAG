import http from './http'

/** 镜像后端 AdminUserOut(app/schemas/admin.py) */
export interface AdminUser {
  id: number
  username: string
  role: 'viewer' | 'editor' | 'admin'
  is_active: boolean
}

export const adminApi = {
  async listUsers(): Promise<AdminUser[]> {
    const { data } = await http.get<AdminUser[]>('/admin/users')
    return data
  },

  async updateUser(
    id: number,
    payload: { role?: AdminUser['role']; is_active?: boolean },
  ): Promise<AdminUser> {
    const { data } = await http.patch<AdminUser>(`/admin/users/${id}`, payload)
    return data
  },
}
