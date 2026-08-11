import type { ApiErrorBody, Job, JobDetail, JobType, Stats } from '../types'

const BASE = '/api'

/** Carries the server's error envelope so the UI can show a real message + request_id. */
export class ApiError extends Error {
  code: string
  details: { field?: string; message: string }[]
  requestId: string | null
  status: number

  constructor(status: number, body: ApiErrorBody | null, fallback: string) {
    super(body?.error?.message ?? fallback)
    this.status = status
    this.code = body?.error?.code ?? 'UNKNOWN'
    this.details = body?.error?.details ?? []
    this.requestId = body?.error?.request_id ?? null
  }

  /** Field-level messages, keyed for direct attachment to form inputs. */
  fieldErrors(): Record<string, string> {
    return Object.fromEntries(
      this.details.filter((d) => d.field).map((d) => [d.field as string, d.message]),
    )
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response
  try {
    res = await fetch(`${BASE}${path}`, {
      ...init,
      headers: { 'Content-Type': 'application/json', ...(init?.headers ?? {}) },
    })
  } catch {
    // Network-level failure never reaches the server, so there's no envelope to parse.
    throw new ApiError(0, null, 'Cannot reach the server. Check your connection.')
  }

  if (!res.ok) {
    const body = await res.json().catch(() => null)
    throw new ApiError(res.status, body, `Request failed (${res.status})`)
  }
  if (res.status === 204) return undefined as T
  return res.json() as Promise<T>
}

export const api = {
  listJobs: (params: { status?: string; limit?: number } = {}) => {
    const q = new URLSearchParams()
    if (params.status) q.set('status', params.status)
    q.set('limit', String(params.limit ?? 100))
    return request<{ items: Job[]; next_cursor: string | null }>(`/jobs?${q}`)
  },

  getJob: (id: string) => request<JobDetail>(`/jobs/${id}`),

  submitJob: (body: {
    job_type: string
    payload: Record<string, unknown>
    priority: number
  }) =>
    request<Job>('/jobs', {
      method: 'POST',
      // A client-generated idempotency key makes a retried submit safe: if the first
      // request actually landed and only the response was lost, the retry returns the
      // original job instead of creating a second one.
      headers: { 'Idempotency-Key': crypto.randomUUID() },
      body: JSON.stringify(body),
    }),

  retryJob: (id: string) => request<Job>(`/jobs/${id}/retry`, { method: 'POST' }),
  cancelJob: (id: string) => request<Job>(`/jobs/${id}/cancel`, { method: 'POST' }),
  jobTypes: () => request<JobType[]>('/job-types'),
  stats: () => request<Stats>('/stats'),
}
