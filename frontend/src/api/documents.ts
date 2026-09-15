import http from './http'

/** 镜像后端 DocumentOut(app/schemas/document.py) */
export interface DocumentItem {
  id: number
  kb_id: number
  filename: string
  mime: string
  size: number
  sha256: string
  status: string
  error_msg: string | null
  page_count: number | null
  chunk_count: number
  created_at: string
}

/** 镜像 GET /documents/{id}/chunks 的条目(app/api/documents.py list_chunks) */
export interface ChunkItem {
  id: number
  chunk_index: number
  page_no: number | null
  char_len: number
  content_preview: string
}

export interface ChunksResponse {
  total: number
  items: ChunkItem[]
}

export const documentsApi = {
  async list(kbId: number): Promise<DocumentItem[]> {
    const { data } = await http.get<DocumentItem[]>(`/kbs/${kbId}/documents`)
    return data
  },

  async upload(
    kbId: number,
    file: File,
    onProgress?: (percent: number) => void,
  ): Promise<DocumentItem> {
    const form = new FormData()
    form.append('file', file)
    const { data } = await http.post<DocumentItem>(`/kbs/${kbId}/documents`, form, {
      onUploadProgress: (e) => {
        if (onProgress && e.total) {
          onProgress(Math.round((e.loaded / e.total) * 100))
        }
      },
    })
    return data
  },

  async detail(docId: number): Promise<DocumentItem> {
    const { data } = await http.get<DocumentItem>(`/documents/${docId}`)
    return data
  },

  async chunks(docId: number, page = 1, pageSize = 20): Promise<ChunksResponse> {
    const { data } = await http.get<ChunksResponse>(`/documents/${docId}/chunks`, {
      params: { page, page_size: pageSize },
    })
    return data
  },

  async reprocess(docId: number): Promise<DocumentItem> {
    const { data } = await http.post<DocumentItem>(`/documents/${docId}/reprocess`)
    return data
  },
}
