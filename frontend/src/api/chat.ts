import http from './http'

/** 镜像后端 ConversationOut(app/schemas/chat.py) */
export interface ConversationItem {
  id: number
  kb_ids: number[]
  title: string
  created_at: string
}

/** 引用条目(app/services/chat_graph/nodes.py build_citations) */
export interface Citation {
  number: number
  chunk_id: number
  document_id: number
  filename: string
  page_no: number | null
  excerpt: string
}

/** 镜像后端 MessageOut(citations 为 list | None) */
export interface MessageItem {
  id: number
  role: string
  content: string
  citations: Citation[] | null
  created_at: string
}

export const conversationsApi = {
  async list(): Promise<ConversationItem[]> {
    const { data } = await http.get<ConversationItem[]>('/chat/conversations')
    return data
  },

  async messages(conversationId: number): Promise<MessageItem[]> {
    const { data } = await http.get<MessageItem[]>(
      `/chat/conversations/${conversationId}/messages`,
    )
    return data
  },

  async remove(conversationId: number): Promise<void> {
    await http.delete(`/chat/conversations/${conversationId}`)
  },

  /** M5:导出会话 markdown(后端拼好含引用附录) */
  async export(conversationId: number): Promise<Blob> {
    const { data } = await http.get<Blob>(`/chat/conversations/${conversationId}/export`, {
      responseType: 'blob',
    })
    return data
  },
}
