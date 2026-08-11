import { useCallback, useEffect, useState } from 'react'
import { api } from './api/client'
import { JobTable } from './components/JobTable'
import { SubmitForm } from './components/SubmitForm'
import { EmptyState, ErrorState, LoadingState } from './components/States'
import { useEventStream } from './hooks/useEventStream'
import type { Job, JobType, Stats } from './types'

const FILTERS = ['all', 'queued', 'running', 'succeeded', 'dead_letter'] as const
type Filter = (typeof FILTERS)[number]

export default function App() {
  const [jobs, setJobs] = useState<Job[] | null>(null)
  const [jobTypes, setJobTypes] = useState<JobType[]>([])
  const [stats, setStats] = useState<Stats | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [filter, setFilter] = useState<Filter>('all')

  const load = useCallback(async () => {
    setError(null)
    try {
      const [list, types] = await Promise.all([
        api.listJobs({ status: filter === 'all' ? undefined : filter }),
        api.jobTypes(),
      ])
      setJobs(list.items)
      setJobTypes(types)
    } catch (err) {
      setError(err)
      setJobs(null)
    }
  }, [filter])

  useEffect(() => {
    void load()
  }, [load])

  // Stats come from the materialised view, which refreshes on a cadence — so polling
  // it slowly is correct. Job rows come over SSE because those must be immediate.
  useEffect(() => {
    let alive = true
    const tick = async () => {
      try {
        const s = await api.stats()
        if (alive) setStats(s)
      } catch {
        /* stats are supplementary; a failure here must not blank the dashboard */
      }
    }
    void tick()
    const id = setInterval(tick, 5000)
    return () => {
      alive = false
      clearInterval(id)
    }
  }, [])

  // Every event carries the job's current status, so the table is patched in place
  // rather than refetched. One round trip saved per event, and no flicker.
  const onEvent = useCallback(() => {
    void api
      .listJobs({ status: filter === 'all' ? undefined : filter })
      .then((list) => setJobs(list.items))
      .catch(() => {
        /* the stream will deliver the next update; don't blank the table */
      })
  }, [filter])

  const { state: connection, lastSeq } = useEventStream(onEvent)

  return (
    <div className="app">
      <header className="topbar">
        <div>
          <h1>Job Orchestrator</h1>
          <p className="dim">Async orchestration with a live operations view</p>
        </div>
        <div className="conn">
          <span className={`dot dot-${connection}`} aria-hidden />
          <span>
            {connection === 'live'
              ? 'Live'
              : connection === 'connecting'
                ? 'Connecting…'
                : 'Reconnecting…'}
          </span>
          {lastSeq > 0 && <span className="dim mono">seq {lastSeq}</span>}
        </div>
      </header>

      {stats && (
        <section className="stats">
          {stats.by_type.length === 0 ? (
            <span className="dim">No statistics yet.</span>
          ) : (
            stats.by_type.map((r) => (
              <div className="stat" key={`${r.job_type}-${r.status}`}>
                <span className="stat-value">{r.job_count}</span>
                <span className="stat-label">
                  {r.job_type} · {r.status}
                </span>
                <span className="dim">p95 {r.p95_duration_ms}ms</span>
              </div>
            ))
          )}
          {/* The read model is allowed to lag; say so rather than implying it's live. */}
          <span className="dim as-of">
            aggregates as of {new Date(stats.as_of).toLocaleTimeString()} (refresh every{' '}
            {stats.stale_after_seconds}s)
          </span>
        </section>
      )}

      <main className="layout">
        <SubmitForm jobTypes={jobTypes} onSubmitted={load} />

        <section className="card">
          <div className="card-head">
            <h2>Jobs</h2>
            <div className="filters">
              {FILTERS.map((f) => (
                <button
                  key={f}
                  className={`chip ${filter === f ? 'chip-on' : ''}`}
                  onClick={() => setFilter(f)}
                >
                  {f.replace('_', ' ')}
                </button>
              ))}
            </div>
          </div>

          {/* The three states, explicitly, in this order. */}
          {error ? (
            <ErrorState error={error} onRetry={load} />
          ) : jobs === null ? (
            <LoadingState label="Loading jobs…" rows={4} />
          ) : jobs.length === 0 ? (
            <EmptyState
              title={filter === 'all' ? 'No jobs yet' : `No ${filter.replace('_', ' ')} jobs`}
              hint={
                filter === 'all'
                  ? 'Submit one using the form to see it move through the pipeline.'
                  : 'Try a different filter.'
              }
            />
          ) : (
            <JobTable jobs={jobs} onChanged={load} />
          )}
        </section>
      </main>
    </div>
  )
}
