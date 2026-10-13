// Fixture copied from tenantguard.yaml (pool, tiers, projects, tenants, brain.noisy) so the mock
// runs with no backend. Only used when VITE_MOCK=1, by scripts/mock-ws-server.mjs, and as the
// store's fallback until the first snapshot names each tenant's project.
import type { PoolSpec, ProjectSpec, Quota, Tier } from '../types.ts'

export interface FixtureTenant {
  id: string
  name: string
  project: string
  monthly_revenue_rs: number
  protected: boolean
}

export const POOL: PoolSpec = { cpu_total: 5.75, mem_total_mb: 2688, headroom_ratio: 0.1 }

export const PROJECTS: ProjectSpec[] = [
  { id: 'health', name: 'Health', tier: 'enterprise', floor_cpu: 1.25, cap_cpu: 3.0, warm_min: 1 },
  { id: 'commerce', name: 'Commerce', tier: 'enterprise', floor_cpu: 1.25, cap_cpu: 3.0, warm_min: 1 },
  { id: 'mobility', name: 'Mobility', tier: 'pro', floor_cpu: 0.75, cap_cpu: 2.0, warm_min: 1 },
  { id: 'media', name: 'Media', tier: 'pro', floor_cpu: 0.75, cap_cpu: 2.0, warm_min: 1 },
  { id: 'free', name: 'Free', tier: 'free', floor_cpu: 0.5, cap_cpu: 1.0, warm_min: 1 },
]

export const TENANTS: FixtureTenant[] = [
  { id: 'A', name: 'Aarogya Health', project: 'health', monthly_revenue_rs: 900000, protected: true },
  { id: 'D', name: 'DhanSetu Payments', project: 'health', monthly_revenue_rs: 1200000, protected: true },
  { id: 'J', name: 'JhatpatMart', project: 'commerce', monthly_revenue_rs: 1500000, protected: false },
  { id: 'B', name: 'BazaarKart', project: 'commerce', monthly_revenue_rs: 250000, protected: false },
  { id: 'G', name: 'GaadiGo', project: 'mobility', monthly_revenue_rs: 800000, protected: false },
  { id: 'K', name: 'KaamKaro', project: 'mobility', monthly_revenue_rs: 180000, protected: false },
  { id: 'E', name: 'Ekalavya Learn', project: 'mobility', monthly_revenue_rs: 200000, protected: false },
  { id: 'H', name: 'HisaabBooks', project: 'media', monthly_revenue_rs: 150000, protected: false },
  { id: 'L', name: 'LokVani News', project: 'media', monthly_revenue_rs: 0, protected: false },
  { id: 'C', name: 'Chitra Studio', project: 'free', monthly_revenue_rs: 0, protected: false },
  { id: 'F', name: 'FitZone', project: 'free', monthly_revenue_rs: 0, protected: false },
  { id: 'I', name: 'IdliExpress', project: 'free', monthly_revenue_rs: 0, protected: false },
]

export const TIERS: Record<Tier, { weight: number; quota: Quota }> = {
  enterprise: { weight: 4, quota: { rate_rps: 100, burst: 200, concurrency: 16 } },
  pro: { weight: 2, quota: { rate_rps: 50, burst: 100, concurrency: 8 } },
  free: { weight: 1, quota: { rate_rps: 20, burst: 40, concurrency: 4 } },
}

/** control.replica_mem in tenantguard.yaml: baseline 80 + cache 8 + 32 per tenant, rounded up to 32 MB. */
export const replicaMemoryMb = (tenants: number) => Math.ceil((80 + 8 + 32 * tenants) / 32) * 32

export const NOISY = {
  warn: 1.5,
  throttle: 2.0,
  quarantine: 4.0,
  recover_below: 1.0,
  hold_s: { warn: 2, throttle: 2, deprioritize: 4, quarantine: 6, recover: 10 },
  throttle_factor: 0.5,
  recover_step: 0.1,
}
