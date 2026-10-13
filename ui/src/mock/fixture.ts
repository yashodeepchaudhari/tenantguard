// Fixture copied from layouts/ag23.json + tenantguard.yaml (tiers, brain.noisy) so the mock
// runs with no backend. Only used when VITE_MOCK=1 or by scripts/mock-ws-server.mjs.
import type { Quota, Tier } from '../types.ts'

export interface FixtureTenant {
  id: string
  name: string
  tier: Tier
  servers: string[]
  monthly_revenue_rs: number
  protected: boolean
  sla_p95_ms: number // end-to-end p95 promised to the tenant (layout)
}

export const SERVERS = ['w0', 'w1', 'w2', 'w3', 'w4', 'w5', 'w6', 'w7', 'w8']

export const TENANTS: FixtureTenant[] = [
  { id: 'A', name: 'Aarogya Health', tier: 'pro', servers: ['w0', 'w1', 'w2'], monthly_revenue_rs: 400000, protected: true, sla_p95_ms: 50 },
  { id: 'B', name: 'BazaarKart', tier: 'pro', servers: ['w3', 'w4', 'w5'], monthly_revenue_rs: 250000, protected: false, sla_p95_ms: 80 },
  { id: 'C', name: 'Chitra Studio', tier: 'free', servers: ['w6', 'w7', 'w8'], monthly_revenue_rs: 0, protected: false, sla_p95_ms: 150 },
  { id: 'D', name: 'DhanSetu Payments', tier: 'enterprise', servers: ['w0', 'w3', 'w6'], monthly_revenue_rs: 1200000, protected: true, sla_p95_ms: 40 },
  { id: 'E', name: 'Ekalavya Learn', tier: 'pro', servers: ['w1', 'w4', 'w7'], monthly_revenue_rs: 200000, protected: false, sla_p95_ms: 80 },
  { id: 'F', name: 'FitZone', tier: 'enterprise', servers: ['w2', 'w5', 'w8'], monthly_revenue_rs: 700000, protected: false, sla_p95_ms: 50 },
  { id: 'G', name: 'GaadiGo', tier: 'free', servers: ['w0', 'w4', 'w8'], monthly_revenue_rs: 0, protected: false, sla_p95_ms: 150 },
  { id: 'H', name: 'HisaabBooks', tier: 'pro', servers: ['w1', 'w5', 'w6'], monthly_revenue_rs: 150000, protected: false, sla_p95_ms: 80 },
  { id: 'I', name: 'IdliExpress', tier: 'free', servers: ['w2', 'w3', 'w7'], monthly_revenue_rs: 0, protected: false, sla_p95_ms: 150 },
  { id: 'J', name: 'JhatpatMart', tier: 'enterprise', servers: ['w0', 'w5', 'w7'], monthly_revenue_rs: 1500000, protected: false, sla_p95_ms: 50 },
  { id: 'K', name: 'KaamKaro', tier: 'pro', servers: ['w1', 'w3', 'w8'], monthly_revenue_rs: 180000, protected: false, sla_p95_ms: 80 },
  { id: 'L', name: 'LokVani News', tier: 'enterprise', servers: ['w2', 'w4', 'w6'], monthly_revenue_rs: 650000, protected: false, sla_p95_ms: 50 },
]

export const TIERS: Record<Tier, { weight: number; quota: Quota }> = {
  enterprise: { weight: 4, quota: { rate_rps: 100, burst: 200, cost_ms_per_s: 600, concurrency: 16 } },
  pro: { weight: 2, quota: { rate_rps: 50, burst: 100, cost_ms_per_s: 300, concurrency: 8 } },
  free: { weight: 1, quota: { rate_rps: 20, burst: 40, cost_ms_per_s: 150, concurrency: 4 } },
}

export const NOISY = {
  warn: 1.5,
  throttle: 2.0,
  quarantine: 4.0,
  recover_below: 1.0,
  hold_s: { warn: 5, throttle: 2, deprioritize: 4, quarantine: 6, recover: 10 },
  throttle_factor: 0.5,
  recover_step: 0.1,
}
