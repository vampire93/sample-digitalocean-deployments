import type { ReactNode } from 'react'
import { ApiError } from '../api/client'

/**
 * The three async states, as shared components.
 *
 * Making them shared rather than ad-hoc per screen is what stops them being forgotten:
 * every async surface in this app renders one of these or real data, never a blank box.
 */

export function LoadingState({ label = 'Loading…', rows = 3 }: { label?: string; rows?: number }) {
  return (
    <div className="state" role="status" aria-live="polite">
      {/* Skeleton rows rather than a spinner on a blank page: the layout doesn't jump
          when data arrives, so the page feels stable instead of flickering. */}
      {Array.from({ length: rows }).map((_, i) => (
        <div className="skeleton" key={i} />
      ))}
      <span className="state-label">{label}</span>
    </div>
  )
}

export function EmptyState({
  title,
  hint,
  action,
}: {
  title: string
  hint?: string
  action?: ReactNode
}) {
  return (
    <div className="state state-empty">
      <p className="state-title">{title}</p>
      {/* An empty state should tell you how to make it non-empty. */}
      {hint && <p className="state-hint">{hint}</p>}
      {action}
    </div>
  )
}

export function ErrorState({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  const api = error instanceof ApiError ? error : null
  const message = error instanceof Error ? error.message : 'Something went wrong'

  return (
    <div className="state state-error" role="alert">
      <p className="state-title">{message}</p>
      {api?.details?.length ? (
        <ul className="state-details">
          {api.details.map((d, i) => (
            <li key={i}>{d.field ? `${d.field}: ${d.message}` : d.message}</li>
          ))}
        </ul>
      ) : null}
      {/* request_id is shown so a user-reported failure maps to a log line without guesswork. */}
      {api?.requestId && <p className="state-hint mono">request id: {api.requestId}</p>}
      {onRetry && (
        <button className="btn" onClick={onRetry}>
          Try again
        </button>
      )}
    </div>
  )
}
