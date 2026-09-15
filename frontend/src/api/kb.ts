import http from './http'

/** 镜像后端 KBOut(app/schemas/kb.py) */
export interface KbItem {
  id: number
  name: string
  description: string | null
  owner_id: number
  embed_provider: string
  embed_model: string
  created_at: string
}

/** 对应后端 KBIn */
export interface KbIn {
  name: string
  description?: string | null
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
}
