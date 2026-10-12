import { createContext, useContext, useSyncExternalStore } from 'react'
import type { Backend } from './backend.ts'
import type { LiveState, LiveStore } from './store.ts'

export interface LiveCtx {
  backend: Backend
  store: LiveStore
}

export const LiveContext = createContext<LiveCtx | null>(null)

function useCtx(): LiveCtx {
  const c = useContext(LiveContext)
  if (!c) throw new Error('LiveProvider missing')
  return c
}

export function useBackend(): Backend {
  return useCtx().backend
}

export function useStore(): LiveStore {
  return useCtx().store
}

export function useLive(): LiveState {
  const { store } = useCtx()
  return useSyncExternalStore(store.subscribe, store.getState)
}

const tickSubscribe = (ms: number) => (cb: () => void) => {
  const id = setInterval(cb, ms)
  return () => clearInterval(id)
}
const sub1s = tickSubscribe(1000)
const nowSeconds = () => Math.floor(Date.now() / 1000)

/** Current epoch second, re-rendering once a second (for "x s ago" and staleness). */
export function useNow(): number {
  return useSyncExternalStore(sub1s, nowSeconds)
}
