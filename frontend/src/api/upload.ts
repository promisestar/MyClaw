/** 文件上传（multipart，不走 axios 默认 JSON Content-Type） */

const API_BASE = import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000/api'

export interface UploadResponse {
  filename: string
  stored_path: string
  size: number
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
}
