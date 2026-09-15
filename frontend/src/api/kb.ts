import http from './http'

/** 镜像后端 KBOut(app/schemas/kb.py);my_perm 为 M4 权限标注 */
export interface KbItem {
  id: number
  name: string
  description: string | null
  owner_id: number
  embed_provider: string
  embed_model: string
  created_at: string
  my_perm?: string | null
}

/** 对应后端 KBIn */
export interface KbIn {
  name: string
  description?: string | null
}

/** 镜像后端 MemberOut(app/schemas/kb.py) */
export interface KbMember {
  user_id: number
  username: string
  perm: 'viewer' | 'editor'
}

export const kbApi = {
  async list(): Promise<KbItem[]> {
    const { data } = await http.get<KbItem[]>('/kbs')
    return data
  },

  async create(payload: KbIn): Promise<KbItem> {
    const { data } = await http.post<KbItem>('/kbs', payload)
    return data
  },

  async detail(kbId: number): Promise<KbItem> {
    const { data } = await http.get<KbItem>(`/kbs/${kbId}`)
    return data
  },

  async members(kbId: number): Promise<KbMember[]> {
    const { data } = await http.get<KbMember[]>(`/kbs/${kbId}/permissions`)
    return data
  },

  async grant(
    kbId: number,
    payload: { username: string; perm: 'viewer' | 'editor' },
  ): Promise<KbMember> {
    const { data } = await http.put<KbMember>(`/kbs/${kbId}/permissions`, payload)
    return data
  },

  async revoke(kbId: number, username: string): Promise<void> {
    await http.delete(`/kbs/${kbId}/permissions`, { params: { username } })
  },
}
