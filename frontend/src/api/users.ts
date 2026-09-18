import http from './http'

/** M9.1:账号搜索条目(仅 id+username;后端过滤停用) */
export interface UserBrief {
  id: number
  username: string
}

export const usersApi = {
  async search(q: string, offset = 0, limit = 20): Promise<UserBrief[]> {
    const { data } = await http.get<UserBrief[]>('/users', {
      params: { q, offset, limit },
    })
    return data
  },
}
