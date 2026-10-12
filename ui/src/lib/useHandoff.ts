import { useEffect, useState } from 'react'
import type { CaseView } from '../types.ts'
import { useBackend } from './liveContext.ts'

export interface HandoffState {
  /** null = bridge unreachable (or not deployed) */
  cases: CaseView[] | null
  error: string | null
}

/** Polls the handoff bridge GET /cases every 5 s; the UI shows its column only when it answers. */
export function useHandoff(): HandoffState {
  const backend = useBackend()
  const [st, setSt] = useState<HandoffState>({ cases: null, error: null })
  useEffect(() => {
    let alive = true
    const load = () =>
      backend
        .handoffCases()
        .then((cases) => alive && setSt({ cases, error: null }))
        .catch((e) => alive && setSt({ cases: null, error: e instanceof Error ? e.message : String(e) }))
    load()
    const id = setInterval(load, 5000)
    return () => {
      alive = false
      clearInterval(id)
    }
  }, [backend])
  return st
}

/** Pending actions with the nearest deadline first. */
export function pendingActions(c: CaseView) {
  return c.actions.filter((a) => a.state === 'pending').sort((a, b) => a.deadline_at - b.deadline_at)
}
