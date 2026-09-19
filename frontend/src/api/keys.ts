import http from './http'

/** API 密钥条目(列表;永不包含明文) */
export interface ApiKeyItem {
  id: number
  name: string
  key_prefix: string
  role: 'read_only' | 'editor'
  is_active: boolean
  kb_scope: number[] | null
  expires_at: string | null
  last_used_at: string | null
  created_at: string
}

export interface ApiKeyCreatePayload {
  name: string
  expires_in_days?: number | null
  role?: 'read_only' | 'editor'
  kb_scope?: number[] | null
}

export interface AdminKeyCreatePayload {
  user_id: number
  name: string
  expires_in_days?: number | null
  role?: 'read_only' | 'editor'
  kb_scope?: number[] | null
}

/** 创建响应:唯一一次携带明文 key */
export interface ApiKeyCreated extends ApiKeyItem {
  key: string
}

export const keysApi = {
  async list(): Promise<ApiKeyItem[]> {
    const { data } = await http.get<ApiKeyItem[]>('/auth/keys')
    return data
  },
  async create(payload: ApiKeyCreatePayload): Promise<ApiKeyCreated> {
    const { data } = await http.post<ApiKeyCreated>('/auth/keys', payload)
    return data
  },
  /** M9.1:admin 为指定账号创建密钥(权限继承目标账号) */
  async createAdmin(payload: AdminKeyCreatePayload): Promise<ApiKeyCreated> {
    const { data } = await http.post<ApiKeyCreated>('/admin/keys', payload)
    return data
  },
  /** M12:admin 代发 scoped key 的范围下拉(目标用户可见库) */
  async adminUserKbs(userId: number): Promise<{ id: number; name: string }[]> {
    const { data } = await http.get(`/admin/users/${userId}/kbs`)
    return data
  },
  async revoke(id: number): Promise<void> {
    await http.delete(`/auth/keys/${id}`)
  },
}
