import { useState } from 'react'
import { ApiError, api } from '../api/client'
import type { JobType } from '../types'

export function SubmitForm({
  jobTypes,
  onSubmitted,
}: {
  jobTypes: JobType[]
  onSubmitted: () => void
}) {
  const [jobType, setJobType] = useState('')
  const [priority, setPriority] = useState(0)
  const [payloadText, setPayloadText] = useState('{\n  "source": "demo"\n}')
  const [submitting, setSubmitting] = useState(false)
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({})
  const [formError, setFormError] = useState<string | null>(null)

  const effectiveType = jobType || jobTypes[0]?.name || ''

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    setFieldErrors({})
    setFormError(null)

    // Validate client-side for fast feedback, but the server validates independently —
    // client validation is a UX affordance, never a security or correctness boundary.
    let payload: Record<string, unknown>
    try {
      payload = JSON.parse(payloadText)
      if (typeof payload !== 'object' || payload === null || Array.isArray(payload)) {
        throw new Error('Payload must be a JSON object')
      }
    } catch (err) {
      setFieldErrors({ payload: err instanceof Error ? err.message : 'Invalid JSON' })
      return
    }

    setSubmitting(true)
    try {
      await api.submitJob({ job_type: effectiveType, payload, priority })
      onSubmitted()
    } catch (err) {
      if (err instanceof ApiError) {
        setFieldErrors(err.fieldErrors())
        setFormError(err.message)
      } else {
        setFormError('Submission failed')
      }
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <form className="card submit-form" onSubmit={handleSubmit}>
      <h2>Submit a job</h2>

      <label htmlFor="job-type">Job type</label>
      <select
        id="job-type"
        value={effectiveType}
        onChange={(e) => setJobType(e.target.value)}
        disabled={submitting || jobTypes.length === 0}
      >
        {jobTypes.map((t) => (
          <option key={t.id} value={t.name}>
            {t.name} — max {t.max_attempts} attempts
          </option>
        ))}
      </select>

      <label htmlFor="priority">Priority ({priority})</label>
      <input
        id="priority"
        type="range"
        min={-10}
        max={10}
        value={priority}
        onChange={(e) => setPriority(Number(e.target.value))}
        disabled={submitting}
      />

      <label htmlFor="payload">Payload (JSON)</label>
      <textarea
        id="payload"
        rows={5}
        className="mono"
        value={payloadText}
        onChange={(e) => setPayloadText(e.target.value)}
        disabled={submitting}
        aria-invalid={Boolean(fieldErrors.payload)}
      />
      {fieldErrors.payload && <p className="field-error">{fieldErrors.payload}</p>}

      <p className="hint">
        Tip: add <code>"force_permanent_failure": true</code> to exercise the
        non-retryable path.
      </p>

      {formError && <p className="field-error">{formError}</p>}

      {/* Disabled while in flight: the visible guard against double submission.
          The idempotency key in the client is the guard against it mattering. */}
      <button className="btn btn-primary" type="submit" disabled={submitting || !effectiveType}>
        {submitting ? 'Submitting…' : 'Submit job'}
      </button>
    </form>
  )
}
