import api from './index'

export interface DocumentInfo {
  source_path: string
  chunk_count: number
  first_content: string
  rag_namespace: string
}

export interface KnowledgeBaseListResponse {
  documents: DocumentInfo[]
  total: number
}

export const knowledgeBaseApi = {
  list: async (namespace?: string) => {
    const params = namespace ? { namespace } : {}
    return api.get<KnowledgeBaseListResponse>('/knowledge-base/list', { params })
  },
  delete: async (sourcePath: string, namespace?: string) => {
    return api.delete('/knowledge-base/document', {
      data: {
        source_path: sourcePath,
        namespace: namespace || 'default',
      },
    })
  },
}
