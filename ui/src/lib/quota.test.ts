import { describe, expect, it } from 'vitest'
import { buildPatch } from './quota.ts'

const q = { rate_rps: 50, burst: 100, cost_ms_per_s: 300, concurrency: 8 }
const d = (o: Partial<Record<keyof typeof q, string>> = {}) => ({
  rate_rps: '50',
  burst: '100',
  cost_ms_per_s: '300',
  concurrency: '8',
  ...o,
})

describe('buildPatch', () => {
  it('sends only changed fields', () => {
    expect(buildPatch(q, d({ rate_rps: '10', concurrency: '4' }))).toEqual({ patch: { rate_rps: 10, concurrency: 4 } })
    expect(buildPatch(q, d())).toEqual({ patch: {} })
  })

  it('rejects zero, negative, empty and non-numbers', () => {
    for (const bad of ['0', '-1', '', 'abc']) expect('error' in buildPatch(q, d({ rate_rps: bad }))).toBe(true)
  })

  it('requires whole numbers for burst and concurrency', () => {
    expect(buildPatch(q, d({ burst: '1.5' }))).toEqual({ error: 'Burst must be a whole number' })
    expect(buildPatch(q, d({ cost_ms_per_s: '12.5' }))).toEqual({ patch: { cost_ms_per_s: 12.5 } })
  })
})
