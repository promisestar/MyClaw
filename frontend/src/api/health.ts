import api from './index'

export interface HealthCheckResult {
  status: 'ok' | 'error' | 'warning' | 'skip'
  latency_ms?: number
  model?: string
  collections?: number
  free_gb?: number
  total_gb?: number
  used_pct?: number
  path?: string
  rss_mb?: number
  available_mb?: number
  reason?: string
  error?: string
}

export interface DetailedHealthResponse {
  status: 'healthy' | 'degraded' | 'unhealthy'
  timestamp: string
  checks: {
    llm: HealthCheckResult
    qdrant: HealthCheckResult
    disk: HealthCheckResult
    memory: HealthCheckResult
  }
}

export const healthApi = {
  getDetailedHealth(): Promise<DetailedHealthResponse> {
    return api.get<DetailedHealthResponse>('/health/detailed')
  },
}
