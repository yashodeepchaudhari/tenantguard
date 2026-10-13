// Admin forms on the Pool page (pure, unit tested): validate drafts before they reach the brain,
// with the same rules the brain enforces (registry.py), so most mistakes never leave the browser.
// The brain still has the last word (sum of floors, memory): its 422 text is shown as it is.
import type { PoolSpec, PoolView, ProjectPatch, ProjectSpec, ProjectView, TenantCreate, Tier } from '../types.ts'

export const PROJECT_ID = /^[a-z0-9-]{1,32}$/
export const TENANT_ID = /^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$/
export const TIER_LIST: Tier[] = ['enterprise', 'pro', 'free']

type Result<K extends string, T> = { [P in K]: T } | { error: string }

const positive = (raw: string, label: string): number | string => {
  const v = Number(raw.trim())
  return raw.trim() !== '' && Number.isFinite(v) && v > 0 ? v : `${label} must be a number > 0`
}

const wholeAtLeast = (raw: string, label: string, min: number): number | string => {
  const v = Number(raw.trim())
  return raw.trim() !== '' && Number.isInteger(v) && v >= min ? v : `${label} must be a whole number >= ${min}`
}

export interface PoolDraft {
  cpu_total: string
  mem_total_mb: string
  headroom_ratio: string
}

export function buildPoolSpec(d: PoolDraft): Result<'spec', PoolSpec> {
  const cpu = positive(d.cpu_total, 'CPU')
  if (typeof cpu === 'string') return { error: cpu }
  const mem = wholeAtLeast(d.mem_total_mb, 'Memory', 1)
  if (typeof mem === 'string') return { error: mem }
  const head = Number(d.headroom_ratio.trim())
  if (d.headroom_ratio.trim() === '' || !Number.isFinite(head) || head < 0 || head >= 1) return { error: 'Headroom must be in [0, 1)' }
  return { spec: { cpu_total: cpu, mem_total_mb: mem, headroom_ratio: head } }
}

export interface ProjectDraft {
  id: string
  name: string
  tier: Tier
  floor_cpu: string
  cap_cpu: string
  warm_min: string
}

function cpuPair(floorRaw: string, capRaw: string): { floor: number; cap: number } | string {
  const floor = positive(floorRaw, 'Floor')
  if (typeof floor === 'string') return floor
  const cap = positive(capRaw, 'Cap')
  if (typeof cap === 'string') return cap
  return floor <= cap ? { floor, cap } : 'Floor must be <= cap'
}

/** `freeCores`: usable pool cores not yet promised as floors; a larger floor would break the pool (I2). */
export function buildProjectSpec(d: ProjectDraft, freeCores = Infinity): Result<'spec', ProjectSpec> {
  const id = d.id.trim()
  if (!PROJECT_ID.test(id)) return { error: 'Id: 1-32 lowercase letters, digits or -' }
  if (!d.name.trim()) return { error: 'Name is required' }
  const cpu = cpuPair(d.floor_cpu, d.cap_cpu)
  if (typeof cpu === 'string') return { error: cpu }
  if (cpu.floor > freeCores + 1e-9) return { error: `Floor ${cpu.floor} cores > ${freeCores.toFixed(2)} cores free for new floors` }
  const warm = wholeAtLeast(d.warm_min, 'Warm replicas', 0)
  if (typeof warm === 'string') return { error: warm }
  return { spec: { id, name: d.name.trim(), tier: d.tier, floor_cpu: cpu.floor, cap_cpu: cpu.cap, warm_min: warm } }
}

/** Changed fields only (an empty patch means nothing to send). */
export function buildProjectPatch(cur: ProjectView, d: Omit<ProjectDraft, 'id'>): Result<'patch', ProjectPatch> {
  if (!d.name.trim()) return { error: 'Name is required' }
  const cpu = cpuPair(d.floor_cpu, d.cap_cpu)
  if (typeof cpu === 'string') return { error: cpu }
  const warm = wholeAtLeast(d.warm_min, 'Warm replicas', 0)
  if (typeof warm === 'string') return { error: warm }
  const next = { name: d.name.trim(), tier: d.tier, floor_cpu: cpu.floor, cap_cpu: cpu.cap, warm_min: warm }
  const patch: ProjectPatch = {}
  for (const k of Object.keys(next) as (keyof typeof next)[]) if (next[k] !== cur[k]) Object.assign(patch, { [k]: next[k] })
  return { patch }
}

export function buildTenantCreate(d: { id: string; name: string; revenue: string }): Result<'req', TenantCreate> {
  const id = d.id.trim()
  if (!TENANT_ID.test(id)) return { error: 'Tenant id: letters, digits, _ or -, up to 32, starting with a letter or digit' }
  if (!d.name.trim()) return { error: 'Name is required' }
  const rev = d.revenue.trim() === '' ? 0 : Number(d.revenue)
  if (!Number.isInteger(rev) || rev < 0) return { error: 'Revenue must be a whole number of rupees >= 0' }
  return { req: { id, name: d.name.trim(), monthly_revenue_rs: rev } }
}

export interface Budget {
  usable: number // cores after headroom
  floors: number // sum of guaranteed floors
  free: number // usable - floors: room for new floors
  headroom: number // cores never promised
  allocated: number // sum of current allocations (base + loan)
  lent: number // sum of loans
}

export function budget(p: PoolView): Budget {
  const allocated = p.projects.reduce((a, x) => a + x.alloc_cpu, 0)
  const lent = p.projects.reduce((a, x) => a + x.loan_cpu, 0)
  return {
    usable: p.usable_cpu,
    floors: p.floors_cpu,
    free: Math.max(0, p.usable_cpu - p.floors_cpu),
    headroom: Math.max(0, p.pool.cpu_total - p.usable_cpu),
    allocated,
    lent,
  }
}
