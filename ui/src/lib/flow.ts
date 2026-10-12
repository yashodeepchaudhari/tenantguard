// Routing model for the flow view (pure, unit tested): where would the gateway send this
// tenant's next request? Mirrors gateway routing: relocation first, else route weights
// (tenantguard mode), else equal weights over the barcode; drained servers are skipped unless
// every candidate is drained (the gateway never black-holes a tenant).
import type { Mode, OverridesView, ServerView } from '../types.ts'

export interface Route {
  server: string
  weight: number // normalized 0..1
}

export function tenantRoutes(tenant: string, barcode: string[], ov: OverridesView | null): Route[] {
  const reloc = ov?.relocations?.find((r) => r.tenant === tenant && !r.cls && r.target)
  if (reloc?.target) return [{ server: reloc.target, weight: 1 }]
  const drained = new Set(Object.entries(ov?.drains ?? {}).filter(([, d]) => d.drained).map(([s]) => s))
  const rw = ov?.route_weights?.[tenant]
  let cand: Route[] = rw ? Object.entries(rw).map(([server, weight]) => ({ server, weight })) : barcode.map((server) => ({ server, weight: 1 }))
  const alive = cand.filter((r) => !drained.has(r.server))
  if (alive.length && alive.some((r) => r.weight > 0)) cand = alive
  const total = cand.reduce((a, r) => a + Math.max(0, r.weight), 0)
  return total > 0 ? cand.map((r) => ({ server: r.server, weight: Math.max(0, r.weight) / total })) : []
}

export function routeTable(tenants: string[], barcodes: Record<string, string[]>, ov: OverridesView | null): Record<string, Route[]> {
  return Object.fromEntries(tenants.map((t) => [t, tenantRoutes(t, barcodes[t] ?? [], ov)]))
}

/** Weighted pick with u in [0, 1). */
export function pickServer(routes: Route[], u: number): string | null {
  let acc = 0
  for (const r of routes) {
    acc += r.weight
    if (u < acc) return r.server
  }
  return routes.length ? routes[routes.length - 1].server : null
}

export interface RerouteHint {
  kind: 'rerouted' | 'refused' | 'off'
  text: string
}

/**
 * Why the flow view does (or does not) show reroute lines right now.
 * During an attack the gateway's limits usually refuse the excess before any server gets slow,
 * so no rescue (rerouting) is needed: say so, instead of letting the missing blue lines look broken.
 */
export function rerouteHint(ov: OverridesView | null, attacking: boolean, mode: Mode, servers: ServerView[]): RerouteHint | null {
  const rw = ov?.route_weights ?? {}
  const steered = new Set([...Object.keys(rw), ...(ov?.relocations ?? []).map((r) => r.tenant)])
  if (steered.size) {
    const avoided = new Set<string>()
    for (const w of Object.values(rw)) for (const [sv, v] of Object.entries(w)) if (v <= 0) avoided.add(sv)
    if (!avoided.size) for (const x of servers) if (x.status === 'slow') avoided.add(x.server)
    const around = avoided.size ? ` around ${[...avoided].sort().join(', ')}` : ''
    return { kind: 'rerouted', text: `↷ ${steered.size} tenant${steered.size === 1 ? '' : 's'} rerouted${around}` }
  }
  if (!attacking) return null
  if (mode === 'off') return { kind: 'off', text: 'mode OFF: no limits and no rerouting, the attack reaches the servers' }
  return { kind: 'refused', text: 'no rerouting needed: the excess is refused at the gateway (red ✕)' }
}
