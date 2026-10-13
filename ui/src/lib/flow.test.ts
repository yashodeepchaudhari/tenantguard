import { describe, expect, it } from 'vitest'
import type { OverridesView, PoolView, ProjectView, ReplicaView, TenantView } from '../types.ts'
import { flowHint, pickServer, routeTable, tenantRoutes } from './flow.ts'

const ov = (o: Partial<OverridesView>): OverridesView => ({ limits: {}, blocks: {}, pins: {}, expires_at: {}, fail_static: false, ...o })
const rep = (id: string, state: ReplicaView['state'] = 'ready', o: Partial<ReplicaView> = {}): ReplicaView => ({
  id, project: id.split('-')[1] ?? 'p', url: `http://${id}`, state, borrowed: false, nano_cpus: 1e9, cpu_shares: 1024, mem_mb: 160, started_at: 0, tenants: [], ...o,
})
const proj = (id: string, replicas: ReplicaView[], o: Partial<ProjectView> = {}): ProjectView => ({
  id, name: id, tier: 'pro', weight: 2, floor_cpu: 0.75, cap_cpu: 2, warm_min: 1, tenants: [], demand_cpu: 0.4, used_cpu: 0.4,
  base_cpu: 0.4, loan_cpu: 0, alloc_cpu: 0.4, state: 'warm', replicas, ...o,
})
const pool = (projects: ProjectView[]): PoolView => ({
  pool: { cpu_total: 5.75, mem_total_mb: 2688, headroom_ratio: 0.1 }, usable_cpu: 5.175, usable_mem_mb: 2419, floors_cpu: 4.5, reserved_mem_mb: 800, projects,
})
const tv = (tenant: string, project: string): TenantView => ({
  tenant, name: tenant, tier: 'pro', p95_ms: 20, rps: 5, rejected_rps: 0, cpu_share: 0.1, noisy_score: 0.5, noisy_norm: 0.1, level: 'normal', factor: 1, project,
})

describe('flow routing model', () => {
  it('a tenant reaches only its own project’s ready replicas, evenly', () => {
    const r = tenantRoutes('K', [rep('tg-mobility-1'), rep('tg-mobility-2'), rep('tg-mobility-3', 'starting'), rep('tg-mobility-4', 'draining')], undefined)
    expect(r).toEqual([
      { server: 'tg-mobility-1', weight: 0.5 },
      { server: 'tg-mobility-2', weight: 0.5 },
    ])
  })

  it('a split-test pin sends everything to the pinned replica; a stale pin is ignored', () => {
    const reps = [rep('tg-free-1'), rep('tg-free-2')]
    expect(tenantRoutes('F', reps, { F: 'tg-free-2' })).toEqual([{ server: 'tg-free-2', weight: 1 }])
    expect(tenantRoutes('F', reps, { F: 'tg-free-9' }).map((x) => x.server)).toEqual(['tg-free-1', 'tg-free-2'])
  })

  it('a sleeping project has no route (its requests wait for a wake)', () => {
    expect(tenantRoutes('C', [], undefined)).toEqual([])
  })

  it('route table keys every tenant by its project', () => {
    const t = routeTable([tv('A', 'health'), tv('K', 'mobility')], pool([proj('health', [rep('tg-health-1')]), proj('mobility', [rep('tg-mobility-1')])]), null)
    expect(t.A.map((r) => r.server)).toEqual(['tg-health-1'])
    expect(t.K.map((r) => r.server)).toEqual(['tg-mobility-1'])
  })

  it('pickServer follows the weights', () => {
    const r = [
      { server: 'a', weight: 0.25 },
      { server: 'b', weight: 0.75 },
    ]
    expect(pickServer(r, 0.1)).toBe('a')
    expect(pickServer(r, 0.5)).toBe('b')
    expect(pickServer(r, 0.9999)).toBe('b')
    expect(pickServer([], 0.5)).toBeNull()
  })
})

describe('flow hint', () => {
  it('a split test names its pinned tenants and replicas', () => {
    expect(flowHint(ov({ pins: { C: 'tg-free-1', F: 'tg-free-2', I: 'tg-free-1' } }), false, 'tenantguard', null)).toEqual({
      kind: 'split',
      text: 'split test: 3 tenants pinned to tg-free-1 / tg-free-2',
    })
  })

  it('lending is shown with the borrowed cores', () => {
    const p = pool([proj('mobility', [], { loan_cpu: 0.345 }), proj('media', [])])
    expect(flowHint(ov({}), false, 'tenantguard', p)).toEqual({ kind: 'lending', text: 'idle CPU lent: mobility +0.34 cores' })
  })

  it('an attack without pins or lending: the gateway refuses the excess; OFF says so', () => {
    expect(flowHint(ov({}), true, 'tenantguard', null)?.kind).toBe('refused')
    expect(flowHint(ov({}), true, 'off', null)?.kind).toBe('off')
    expect(flowHint(ov({}), false, 'tenantguard', null)).toBeNull()
    expect(flowHint(null, false, 'tenantguard', null)).toBeNull()
  })
})
