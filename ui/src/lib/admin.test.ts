import { describe, expect, it } from 'vitest'
import type { PoolView, ProjectView } from '../types.ts'
import { budget, buildPoolSpec, buildProjectPatch, buildProjectSpec, buildTenantCreate } from './admin.ts'

const proj: ProjectView = {
  id: 'media', name: 'Media', tier: 'pro', weight: 2, floor_cpu: 0.75, cap_cpu: 2, warm_min: 1, tenants: ['H', 'L'], demand_cpu: 0.3,
  used_cpu: 0.3, base_cpu: 0.3, loan_cpu: 0, alloc_cpu: 0.3, state: 'warm', replicas: [],
}

describe('pool form', () => {
  it('accepts real capacity and rejects nonsense', () => {
    expect(buildPoolSpec({ cpu_total: '5.75', mem_total_mb: '2688', headroom_ratio: '0.1' })).toEqual({ spec: { cpu_total: 5.75, mem_total_mb: 2688, headroom_ratio: 0.1 } })
    expect(buildPoolSpec({ cpu_total: '0', mem_total_mb: '2688', headroom_ratio: '0.1' })).toEqual({ error: 'CPU must be a number > 0' })
    expect(buildPoolSpec({ cpu_total: '4', mem_total_mb: '10.5', headroom_ratio: '0.1' })).toEqual({ error: 'Memory must be a whole number >= 1' })
    expect('error' in buildPoolSpec({ cpu_total: '4', mem_total_mb: '1024', headroom_ratio: '1' })).toBe(true)
  })
})

describe('project form', () => {
  const d = { id: 'games', name: 'Games', tier: 'free' as const, floor_cpu: '0.25', cap_cpu: '1', warm_min: '0' }
  it('builds a spec with the brain’s id rule and floor <= cap', () => {
    expect(buildProjectSpec(d)).toEqual({ spec: { id: 'games', name: 'Games', tier: 'free', floor_cpu: 0.25, cap_cpu: 1, warm_min: 0 } })
    expect(buildProjectSpec({ ...d, id: 'Bad Id' })).toEqual({ error: 'Id: 1-32 lowercase letters, digits or -' })
    expect(buildProjectSpec({ ...d, floor_cpu: '2' })).toEqual({ error: 'Floor must be <= cap' })
    expect(buildProjectSpec({ ...d, warm_min: '1.5' })).toEqual({ error: 'Warm replicas must be a whole number >= 0' })
    expect(buildProjectSpec({ ...d, floor_cpu: '0.75' }, 0.675)).toEqual({ error: 'Floor 0.75 cores > 0.68 cores free for new floors' })
  })
  it('a patch carries only what changed', () => {
    expect(buildProjectPatch(proj, { name: 'Media', tier: 'pro', floor_cpu: '1', cap_cpu: '2', warm_min: '1' })).toEqual({ patch: { floor_cpu: 1 } })
    expect(buildProjectPatch(proj, { name: 'Media', tier: 'pro', floor_cpu: '0.75', cap_cpu: '2', warm_min: '1' })).toEqual({ patch: {} })
  })
})

describe('tenant form', () => {
  it('uses the worker’s tenant id rule', () => {
    expect(buildTenantCreate({ id: 'M2', name: 'Mitra', revenue: '' })).toEqual({ req: { id: 'M2', name: 'Mitra', monthly_revenue_rs: 0 } })
    expect('error' in buildTenantCreate({ id: '-x', name: 'x', revenue: '' })).toBe(true)
    expect('error' in buildTenantCreate({ id: 'M3', name: 'x', revenue: '-5' })).toBe(true)
  })
})

describe('budget', () => {
  it('splits the pool into floors, free room, headroom and loans', () => {
    const p: PoolView = {
      pool: { cpu_total: 5.75, mem_total_mb: 2688, headroom_ratio: 0.1 }, usable_cpu: 5.175, usable_mem_mb: 2419, floors_cpu: 4.5, reserved_mem_mb: 864,
      projects: [{ ...proj, alloc_cpu: 1.095, base_cpu: 0.75, loan_cpu: 0.345 }],
    }
    const b = budget(p)
    expect(b.free).toBeCloseTo(0.675)
    expect(b.headroom).toBeCloseTo(0.575)
    expect(b.lent).toBeCloseTo(0.345)
    expect(b.allocated).toBeCloseTo(1.095)
  })
})
