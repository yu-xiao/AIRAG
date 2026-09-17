import http from './http'

/** 镜像后端 AdminUserOut(app/schemas/admin.py) */
export interface AdminUser {
  id: number
  username: string
  role: 'viewer' | 'editor' | 'admin'
  is_active: boolean
}

/** 镜像后端 AuditLogOut(app/schemas/admin.py) */
export interface AuditLogItem {
  id: number
  username: string
  action: string
  target: string
  detail: string | null
  ip: string | null
  created_at: string
}

export interface AuditLogQuery {
  username?: string
  action?: string
  page?: number
  page_size?: number
}

export interface AuditLogResponse {
  total: number
  items: AuditLogItem[]
}

/** 镜像后端 POST /admin/audit-logs/purge 响应 */
export interface AuditPurgeResult {
  deleted: number
  retention_days: number
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

  async listAuditLogs(params: AuditLogQuery): Promise<AuditLogResponse> {
    const { data } = await http.get<AuditLogResponse>('/admin/audit-logs', { params })
    return data
  },

  async purgeExpiredLogs(): Promise<AuditPurgeResult> {
    const { data } = await http.post<AuditPurgeResult>('/admin/audit-logs/purge')
    return data
  },
}
