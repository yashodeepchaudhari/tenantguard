import { useEffect, useState } from 'react'
import { useBackend, useLive, useStore } from '../lib/liveContext.ts'
import { MODE_LABEL } from '../lib/status.ts'
import { MODES, type Mode } from '../types.ts'

const HINT: Record<Mode, string> = {
  off: 'FIFO, no limits, static routing',
  industry: 'Token + cost + concurrency limits, weighted fair queue',
  tenantguard: 'Everything, plus the brain: detect, throttle, recover',
}

export function ModeToggle() {
  const backend = useBackend()
  const store = useStore()
  const { snapshot, pendingMode } = useLive()
  const [fetched, setFetched] = useState<Mode | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  // The snapshot carries the mode; ask the gateway directly until the brain stream is up.
  useEffect(() => {
    if (snapshot) return
    let alive = true
    backend
      .getMode()
      .then((m) => alive && setFetched(m))
      .catch(() => {})
    return () => {
      alive = false
    }
  }, [backend, snapshot])

  const current = pendingMode ?? snapshot?.mode ?? fetched

  const choose = async (mode: Mode) => {
    if (mode === current || busy) return
    setBusy(true)
    setError(null)
    store.setPendingMode(mode)
    try {
      const m = await backend.setMode(mode)
      setFetched(m)
      // the next snapshot confirms it and clears pendingMode; don't wait forever
      setTimeout(() => store.getState().pendingMode === mode && store.setPendingMode(null), 3000)
    } catch (e) {
      store.setPendingMode(null)
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="flex flex-col items-end">
      <div className="flex rounded-xl border border-ink-700 bg-ink-900 p-1" role="radiogroup" aria-label="Gateway mode">
        {MODES.map((m) => {
          const active = m === current
          return (
            <button
              key={m}
              role="radio"
              aria-checked={active}
              title={HINT[m]}
              disabled={busy}
              onClick={() => choose(m)}
              className={`rounded-lg px-4 py-1.5 text-lg font-bold transition ${
                active ? 'bg-accent text-ink-950' : 'text-ink-300 hover:text-ink-100'
              } ${active && pendingMode === m ? 'animate-pulse' : ''}`}
            >
              {MODE_LABEL[m]}
            </button>
          )
        })}
      </div>
      {error && (
        <div className="mt-1 max-w-md text-right text-sm text-bad" role="alert">
          Mode change failed: {error}
        </div>
      )}
    </div>
  )
}
