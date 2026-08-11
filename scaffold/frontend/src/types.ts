export type JobStatus =
  | 'queued'
  | 'running'
  | 'succeeded'
  | 'failed'
  | 'dead_letter'
  | 'cancelled'

export interface Job {
  id: string
  job_type: string
  status: JobStatus
  priority: number
  payload: Record<string, unknown>
  idempotency_key: string | null
  attempts: number
  max_attempts: number
  run_after: string
  last_error: string | null
  result: Record<string, unknown> | null
  created_at: string
  updated_at: string
  started_at: string | null
  finished_at: string | null
}

export interface Attempt {
  attempt_no: number
  worker_id: string
  status: string
  error: string | null
  started_at: string
  finished_at: string | null
  duration_ms: number | null
}

export interface JobDetail extends Job {
  attempts_log: Attempt[]
}

export interface JobType {
  id: number
  name: string
  description: string
  max_attempts: number
}

export interface StatsRow {
  job_type: string
  status: string
  job_count: number
  p95_duration_ms: number
  total_attempts: number
}

export interface Stats {
  by_type: StatsRow[]
  total: number
  as_of: string
  stale_after_seconds: number
}

/** Mirrors the backend's single error envelope. */
export interface ApiErrorBody {
  error: {
    code: string
    message: string
    details: { field?: string; message: string }[]
    request_id: string | null
  }
}

export interface JobEvent {
  seq: number
  job_id: string
  job_type: string
  event_type: string
  status: JobStatus
  data: Record<string, unknown>
  created_at: string
}
