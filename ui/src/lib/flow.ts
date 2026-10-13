// Routing model for the flow view (pure, unit tested): where does the gateway send this tenant's
// next request? A tenant only ever goes to its own project's ready replicas. A split-test pin wins;
// otherwise the gateway's rendezvous hash keeps each tenant on one replica (spilling only when it is
// full). The UI cannot compute the gateway's blake2b ranking, so it spreads a tenant evenly over its
// project's ready replicas: the picture shows which replicas a tenant can reach, not the exact one.
import type { Mode, OverridesView, PoolView, ReplicaView, TenantView } from '../types.ts'

export interface Route {
  server: string // replica id
  weight: number // normalized 0..1
}

export const ready = (rs: ReplicaView[] = []) => rs.filter((r) => r.state === 'ready')

export function tenantRoutes(tenant: string, replicas: ReplicaView[], pins: Record<string, string> | undefined): Route[] {
  const ids = ready(replicas).map((r) => r.id)
  const pin = pins?.[tenant]
  if (pin && ids.includes(pin)) return [{ server: pin, weight: 1 }]
  return ids.map((server) => ({ server, weight: 1 / ids.length }))
}

export function routeTable(tenants: TenantView[], pool: PoolView | null | undefined, ov: OverridesView | null): Record<string, Route[]> {
  const byProject = new Map((pool?.projects ?? []).map((p) => [p.id, p.replicas]))
  return Object.fromEntries(tenants.map((t) => [t.tenant, tenantRoutes(t.tenant, byProject.get(t.project) ?? [], ov?.pins)]))
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

export interface FlowHint {
  kind: 'split' | 'lending' | 'refused' | 'off'
  text: string
}

/** One line under the flow view that says what the brain is doing to the routing right now. */
export function flowHint(ov: OverridesView | null, attacking: boolean, mode: Mode, pool: PoolView | null | undefined): FlowHint | null {
  const pins = Object.entries(ov?.pins ?? {})
  if (pins.length) {
    const targets = [...new Set(pins.map(([, r]) => r))].sort()
    return { kind: 'split', text: `split test: ${pins.length} tenant${pins.length === 1 ? '' : 's'} pinned to ${targets.join(' / ')}` }
  }
  const lending = (pool?.projects ?? []).filter((p) => p.loan_cpu > 0.005)
  if (lending.length) {
    const what = lending.map((p) => `${p.id} +${p.loan_cpu.toFixed(2)} cores`).join(', ')
    return { kind: 'lending', text: `idle CPU lent: ${what}` }
  }
  if (!attacking) return null
  if (mode === 'off') return { kind: 'off', text: 'mode OFF: no limits, the attack reaches its project’s replicas' }
  return { kind: 'refused', text: 'the excess is refused at the gateway (red ✕); other projects never see it' }
}
