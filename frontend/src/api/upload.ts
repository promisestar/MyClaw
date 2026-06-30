/** 文件上传（multipart，不走 axios 默认 JSON Content-Type） */

const API_BASE = import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000/api'

/** 与后端 src/api/upload.py 的 UploadResponse 对齐 */
export interface UploadResponse {
  filename: string
  stored_path: string
  size: number
  mime_type: string
  /** 服务端按扩展名分类的大类，用于前端选择缩略图/文件卡渲染 */
  kind: 'image' | 'doc' | 'other'
  /** 文档抽取后的字符数；非文档为 null */
  extracted_chars: number | null
}

/** 与后端 /upload/extract 的返回对齐（文档预览） */
export interface ExtractResponse {
  stored_path: string
  kind: string
  chars: number
  truncated: boolean
  preview: string
  error: string | null
}

async function parseHttpError(res: Response): Promise<string> {
  try {
    const j = (await res.json()) as { detail?: unknown }
    const d = j.detail
    if (typeof d === 'string') return d
    if (Array.isArray(d)) {
      return d
        .map((x: { msg?: string }) => x?.msg)
        .filter(Boolean)
        .join('; ')
    }
    return res.statusText
  } catch {
    return res.statusText
  }
}

export const uploadApi = {
  /**
   * POST /api/upload/file
   * 表单字段：file（必填）、session_id（可选）
   */
  async uploadFile(file: File, sessionId?: string | null): Promise<UploadResponse> {
    const form = new FormData()
    form.append('file', file)
    if (sessionId) {
      form.append('session_id', sessionId)
    }
    const res = await fetch(`${API_BASE}/upload/file`, {
      method: 'POST',
      body: form,
    })
    if (!res.ok) {
      const msg = await parseHttpError(res)
      throw new Error(msg || `上传失败 (${res.status})`)
    }
    return res.json() as Promise<UploadResponse>
  },

  /** GET /api/upload/extract?path=...&preview_chars=... 文档预览（不入历史） */
  async extractPreview(storedPath: string, previewChars = 2000): Promise<ExtractResponse> {
    const params = new URLSearchParams({
      path: storedPath,
      preview_chars: String(previewChars),
    })
    const res = await fetch(`${API_BASE}/upload/extract?${params.toString()}`, {
      method: 'GET',
    })
    if (!res.ok) {
      const msg = await parseHttpError(res)
      throw new Error(msg || `预览失败 (${res.status})`)
    }
    return res.json() as Promise<ExtractResponse>
  },
}
