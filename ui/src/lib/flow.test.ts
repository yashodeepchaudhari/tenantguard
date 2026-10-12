import { describe, expect, it } from 'vitest'
import type { OverridesView } from '../types.ts'
import { pickServer, tenantRoutes } from './flow.ts'

const ov = (o: Partial<OverridesView>): OverridesView => ({
  limits: {}, blocks: {}, relocations: [], route_weights: {}, drains: {}, expires_at: {}, fail_static: false, ...o,
})

describe('flow routing model', () => {
  it('equal weights over the barcode by default', () => {
    expect(tenantRoutes('B', ['w3', 'w4', 'w5'], null).map((r) => r.weight)).toEqual([1 / 3, 1 / 3, 1 / 3])
  })

  it('route weights (rescue) shift traffic, normalized', () => {
    const r = tenantRoutes('B', ['w3', 'w4', 'w5'], ov({ route_weights: { B: { w3: 0, w4: 2, w5: 2 } } }))
    expect(r).toEqual([
      { server: 'w3', weight: 0 },
      { server: 'w4', weight: 0.5 },
      { server: 'w5', weight: 0.5 },
    ])
  })

  it('a whole-tenant relocation sends everything to the target', () => {
    expect(tenantRoutes('K', ['w1', 'w3', 'w8'], ov({ relocations: [{ tenant: 'K', target: 'spare', ttl_s: 60 }] }))).toEqual([{ server: 'spare', weight: 1 }])
    // a class-only relocation does not move the whole tenant
    expect(tenantRoutes('K', ['w1'], ov({ relocations: [{ tenant: 'K', cls: 'abcd1234', target: 'spare', ttl_s: 60 }] }))).toEqual([{ server: 'w1', weight: 1 }])
  })

  it('drained servers are skipped, but never all of them', () => {
    const d = { w3: { server: 'w3', drained: true, ttl_s: 60 } }
    expect(tenantRoutes('B', ['w3', 'w4'], ov({ drains: d })).map((r) => r.server)).toEqual(['w4'])
    expect(tenantRoutes('X', ['w3'], ov({ drains: d })).map((r) => r.server)).toEqual(['w3'])
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
