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

/** 镜像后端 WebhookOut(app/schemas/admin.py,M17);secret 仅露尾 4 */
export interface WebhookEndpoint {
  id: number
  name: string
  url: string
  events: string[] | null
  enabled: boolean
  description: string | null
  secret_masked: string
  created_at: string
}

/** 创建 / 轮换时一次性返回明文 secret,此后不再可见 */
export type WebhookCreated = WebhookEndpoint & { secret: string }

/** 镜像后端 WebhookDeliveryOut(app/schemas/admin.py) */
export interface WebhookDeliveryRow {
  id: number
  endpoint_id: number
  endpoint_name: string
  event_type: string
  status: 'pending' | 'retrying' | 'succeeded' | 'dead'
  attempts: number
  response_status: number | null
  last_error: string | null
  payload: Record<string, unknown> | null
  next_attempt_at: string | null
  created_at: string
}

export interface WebhookDeliveryQuery {
  endpoint_id?: number
  event_type?: string
  status?: string
  page?: number
  page_size?: number
}

export interface WebhookDeliveryResponse {
  total: number
  items: WebhookDeliveryRow[]
}

/** POST /admin/webhooks/{id}/test 响应 */
export interface WebhookTestResult {
  status: string
  response_status: number | null
  error: string | null
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

  // ---- M17:出站 webhook 管理(secret 铁律:明文仅 create/rotate 响应一次) ----

  async listWebhooks(): Promise<WebhookEndpoint[]> {
    const { data } = await http.get<WebhookEndpoint[]>('/admin/webhooks')
    return data
  },

  async createWebhook(payload: {
    name: string
    url: string
    events?: string[]
    description?: string
    secret?: string
  }): Promise<WebhookCreated> {
    const { data } = await http.post<WebhookCreated>('/admin/webhooks', payload)
    return data
  },

  async updateWebhook(
    id: number,
    payload: {
      name?: string
      url?: string
      events?: string[]
      enabled?: boolean
      description?: string
      rotate_secret?: boolean
    },
  ): Promise<WebhookEndpoint | WebhookCreated> {
    const { data } = await http.put<WebhookEndpoint | WebhookCreated>(
      `/admin/webhooks/${id}`,
      payload,
    )
    return data
  },

  async deleteWebhook(id: number): Promise<void> {
    await http.delete(`/admin/webhooks/${id}`)
  },

  async testWebhook(id: number): Promise<WebhookTestResult> {
    const { data } = await http.post<WebhookTestResult>(`/admin/webhooks/${id}/test`)
    return data
  },

  async listWebhookDeliveries(
    params: WebhookDeliveryQuery,
  ): Promise<WebhookDeliveryResponse> {
    const { data } = await http.get<WebhookDeliveryResponse>(
      '/admin/webhook-deliveries',
      { params },
    )
    return data
  },
}
