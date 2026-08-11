import { useEffect, useRef, useState } from 'react'
import type { JobEvent } from '../types'

export type ConnectionState = 'connecting' | 'live' | 'reconnecting'

/**
 * Subscribes to the server's SSE stream.
 *
 * Uses the native EventSource rather than a hand-rolled fetch stream on purpose: the
 * browser handles reconnection *and* resends `Last-Event-ID` automatically, so after a
 * dropped connection the server replays exactly the events we missed. Re-implementing
 * that correctly is more code and more bugs.
 *
 * The connection state is surfaced so the operator can tell "nothing is happening"
 * apart from "we lost the stream" — silently stale dashboards are how people make
 * decisions on old data.
 */
export function useEventStream(onEvent: (e: JobEvent) => void) {
  const [state, setState] = useState<ConnectionState>('connecting')
  const [lastSeq, setLastSeq] = useState<number>(0)
  const handlerRef = useRef(onEvent)
  handlerRef.current = onEvent

  useEffect(() => {
    const es = new EventSource('/api/events')

    es.onopen = () => setState('live')
    es.onerror = () => {
      // EventSource retries on its own; reflect that rather than tearing it down.
      setState(es.readyState === EventSource.CLOSED ? 'reconnecting' : 'reconnecting')
    }

    const onMessage = (ev: MessageEvent) => {
      try {
        const parsed = JSON.parse(ev.data) as JobEvent
        setState('live')
        setLastSeq(parsed.seq)
        handlerRef.current(parsed)
      } catch {
        // A malformed frame must not kill the stream.
      }
    }

    // The server names each event after its type, so subscribe to each explicitly.
    const types = [
      'job.created', 'job.started', 'job.succeeded', 'job.failed',
      'job.dead_lettered', 'job.lease_expired', 'job.retry_requested', 'job.cancelled',
    ]
    types.forEach((t) => es.addEventListener(t, onMessage as EventListener))
    es.onmessage = onMessage

    return () => {
      types.forEach((t) => es.removeEventListener(t, onMessage as EventListener))
      es.close()
    }
  }, [])

  return { state, lastSeq }
}
