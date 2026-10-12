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
}

export const SERVERS = ['w0', 'w1', 'w2', 'w3', 'w4', 'w5', 'w6', 'w7', 'w8']

export const TENANTS: FixtureTenant[] = [
  { id: 'A', name: 'Aarogya Health', tier: 'enterprise', servers: ['w0', 'w1', 'w2'], monthly_revenue_rs: 900000, protected: true },
  { id: 'B', name: 'BazaarKart', tier: 'pro', servers: ['w3', 'w4', 'w5'], monthly_revenue_rs: 250000, protected: false },
  { id: 'C', name: 'Chitra Studio', tier: 'free', servers: ['w6', 'w7', 'w8'], monthly_revenue_rs: 0, protected: false },
  { id: 'D', name: 'DhanSetu Payments', tier: 'enterprise', servers: ['w0', 'w3', 'w6'], monthly_revenue_rs: 1200000, protected: true },
  { id: 'E', name: 'Ekalavya Learn', tier: 'pro', servers: ['w1', 'w4', 'w7'], monthly_revenue_rs: 200000, protected: false },
  { id: 'F', name: 'FitZone', tier: 'free', servers: ['w2', 'w5', 'w8'], monthly_revenue_rs: 0, protected: false },
  { id: 'G', name: 'GaadiGo', tier: 'enterprise', servers: ['w0', 'w4', 'w8'], monthly_revenue_rs: 800000, protected: false },
  { id: 'H', name: 'HisaabBooks', tier: 'pro', servers: ['w1', 'w5', 'w6'], monthly_revenue_rs: 150000, protected: false },
  { id: 'I', name: 'IdliExpress', tier: 'free', servers: ['w2', 'w3', 'w7'], monthly_revenue_rs: 0, protected: false },
  { id: 'J', name: 'JhatpatMart', tier: 'enterprise', servers: ['w0', 'w5', 'w7'], monthly_revenue_rs: 1500000, protected: false },
  { id: 'K', name: 'KaamKaro', tier: 'pro', servers: ['w1', 'w3', 'w8'], monthly_revenue_rs: 180000, protected: false },
  { id: 'L', name: 'LokVani News', tier: 'free', servers: ['w2', 'w4', 'w6'], monthly_revenue_rs: 0, protected: false },
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
  hold_s: { warn: 2, throttle: 2, deprioritize: 4, quarantine: 6, recover: 10 },
  throttle_factor: 0.5,
  recover_step: 0.1,
}
