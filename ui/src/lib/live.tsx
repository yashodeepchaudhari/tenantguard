import { useEffect, useMemo, type ReactNode } from 'react'
import { config } from '../config.ts'
import { createMockBackend } from '../mock/mockBackend.ts'
import { realBackend } from './backend.ts'
import { LiveContext, type LiveCtx } from './liveContext.ts'
import { LiveStore } from './store.ts'

export function LiveProvider({ children }: { children: ReactNode }) {
  const value = useMemo<LiveCtx>(() => {
    const script = new URLSearchParams(window.location.search).get('script') !== '0'
    return {
      backend: config.mock ? createMockBackend({ script }) : realBackend,
      store: new LiveStore({ historyS: config.historyS, sloMs: config.p95WarnMs, warnFallback: config.noisy.warn }),
    }
  }, [])

  useEffect(() => {
    const { backend, store } = value
    let cancelled = false
    backend.incidents().then((list) => !cancelled && store.addIncidents(list))
    const unsub = backend.subscribe(
      (m) => store.apply(m),
      (s) => store.setConn(s),
    )
    // gateway admin data shared by the grid, flow view, impact meter and case files
    const loadTenants = () =>
      backend
        .tenants()
        .then((t) => !cancelled && store.setAdmin(t))
        .catch(() => {})
    const loadOverrides = () =>
      backend
        .overrides()
        .then((o) => !cancelled && store.setOverrides(o))
        .catch(() => !cancelled && store.setOverrides(null))
    loadTenants()
    loadOverrides()
    const t1 = setInterval(loadTenants, 30000)
    const t2 = setInterval(loadOverrides, 2000)
    return () => {
      cancelled = true
      unsub()
      clearInterval(t1)
      clearInterval(t2)
    }
  }, [value])

  return <LiveContext.Provider value={value}>{children}</LiveContext.Provider>
}
