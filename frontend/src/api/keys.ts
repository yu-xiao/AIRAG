import http from './http'

/** API 密钥条目(列表;永不包含明文) */
export interface ApiKeyItem {
  id: number
  name: string
  key_prefix: string
  is_active: boolean
  expires_at: string | null
  last_used_at: string | null
  created_at: string
}

export interface ApiKeyCreatePayload {
  name: string
  expires_in_days?: number | null
}

export interface AdminKeyCreatePayload {
  user_id: number
  name: string
  expires_in_days?: number | null
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
  async revoke(id: number): Promise<void> {
    await http.delete(`/auth/keys/${id}`)
  },
}
