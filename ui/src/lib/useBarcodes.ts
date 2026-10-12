import { useEffect, useState } from 'react'
import { TENANTS } from '../mock/fixture.ts'
import { useBackend } from './liveContext.ts'

const LAYOUT: Record<string, string[]> = Object.fromEntries(TENANTS.map((t) => [t.id, t.servers]))

/** tenant -> its barcode servers, from gateway /admin/tenants; falls back to the ag23 layout. */
export function useBarcodes(): Record<string, string[]> {
  const backend = useBackend()
  const [map, setMap] = useState<Record<string, string[]>>(LAYOUT)
  useEffect(() => {
    let alive = true
    const load = () =>
      backend
        .tenants()
        .then((ts) => alive && ts.length && setMap(Object.fromEntries(ts.map((t) => [t.id, t.servers]))))
        .catch(() => {})
    load()
    const id = setInterval(load, 30000)
    return () => {
      alive = false
      clearInterval(id)
    }
  }, [backend])
  return map
}
