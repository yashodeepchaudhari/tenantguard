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
      store: new LiveStore(config.historyS),
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
    return () => {
      cancelled = true
      unsub()
    }
  }, [value])

  return <LiveContext.Provider value={value}>{children}</LiveContext.Provider>
}
