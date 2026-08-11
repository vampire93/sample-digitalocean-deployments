import { Fragment, useState } from 'react'
import { api } from '../api/client'
import type { Job, JobDetail } from '../types'

const STATUS_LABEL: Record<string, string> = {
  queued: 'Queued',
  running: 'Running',
  succeeded: 'Succeeded',
  failed: 'Retrying',
  dead_letter: 'Dead letter',
  cancelled: 'Cancelled',
}

function relative(iso: string): string {
  const secs = Math.round((Date.now() - new Date(iso).getTime()) / 1000)
  if (secs < 60) return `${secs}s ago`
  if (secs < 3600) return `${Math.floor(secs / 60)}m ago`
  return `${Math.floor(secs / 3600)}h ago`
}

export function JobTable({ jobs, onChanged }: { jobs: Job[]; onChanged: () => void }) {
  const [expanded, setExpanded] = useState<string | null>(null)
  const [detail, setDetail] = useState<JobDetail | null>(null)
  const [busy, setBusy] = useState<string | null>(null)

  async function toggle(id: string) {
    if (expanded === id) {
      setExpanded(null)
      setDetail(null)
      return
    }
    setExpanded(id)
    setDetail(null)
    try {
      setDetail(await api.getJob(id))
    } catch {
      setDetail(null)
    }
  }

  async function act(id: string, action: 'retry' | 'cancel') {
    setBusy(id)
    try {
      await (action === 'retry' ? api.retryJob(id) : api.cancelJob(id))
      onChanged()
    } catch {
      // The row's status is authoritative and arrives via SSE regardless; a failed
      // action simply leaves it unchanged.
    } finally {
      setBusy(null)
    }
  }

  return (
    <div className="table-wrap">
      <table className="jobs">
        <thead>
          <tr>
            <th>Type</th>
            <th>Status</th>
            <th>Attempts</th>
            <th>Created</th>
            <th>Last error</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {jobs.map((j) => (
            <Fragment key={j.id}>
              <tr onClick={() => toggle(j.id)} className="row">
                <td className="mono">{j.job_type}</td>
                <td>
                  <span className={`badge badge-${j.status}`}>
                    {STATUS_LABEL[j.status] ?? j.status}
                  </span>
                </td>
                <td>
                  {j.attempts}/{j.max_attempts}
                </td>
                <td className="dim">{relative(j.created_at)}</td>
                <td className="dim truncate" title={j.last_error ?? ''}>
                  {j.last_error ?? '—'}
                </td>
                <td onClick={(e) => e.stopPropagation()}>
                  {j.status === 'dead_letter' && (
                    <button
                      className="btn btn-sm"
                      disabled={busy === j.id}
                      onClick={() => act(j.id, 'retry')}
                    >
                      Retry
                    </button>
                  )}
                  {j.status === 'queued' && (
                    <button
                      className="btn btn-sm"
                      disabled={busy === j.id}
                      onClick={() => act(j.id, 'cancel')}
                    >
                      Cancel
                    </button>
                  )}
                </td>
              </tr>
              {expanded === j.id && (
                <tr>
                  <td colSpan={6} className="detail">
                    {!detail ? (
                      <span className="dim">Loading attempts…</span>
                    ) : detail.attempts_log.length === 0 ? (
                      <span className="dim">No attempts yet — still queued.</span>
                    ) : (
                      <ol className="timeline">
                        {detail.attempts_log.map((a) => (
                          <li key={a.attempt_no}>
                            <span className={`badge badge-${a.status}`}>#{a.attempt_no}</span>
                            <span className="mono dim">{a.worker_id}</span>
                            <span>{a.duration_ms != null ? `${a.duration_ms}ms` : '—'}</span>
                            {a.error && <span className="err">{a.error}</span>}
                          </li>
                        ))}
                      </ol>
                    )}
                  </td>
                </tr>
              )}
            </Fragment>
          ))}
        </tbody>
      </table>
    </div>
  )
}
