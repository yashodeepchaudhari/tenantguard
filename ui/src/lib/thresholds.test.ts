import { describe, expect, it } from 'vitest'
import { noisyThresholds } from './thresholds.ts'

const fb = { warn: 1.5, throttle: 2, quarantine: 4 }

describe('noisyThresholds', () => {
  it('uses the brain values when present', () => {
    expect(noisyThresholds({ noisy_thresholds: { warn: 1.2, throttle: 3, quarantine: 6 } }, fb)).toEqual({ warn: 1.2, throttle: 3, quarantine: 6 })
  })
  it('falls back per key when missing or invalid', () => {
    expect(noisyThresholds({ noisy_thresholds: { warn: 1.2, quarantine: 0 } }, fb)).toEqual({ warn: 1.2, throttle: 2, quarantine: 4 })
    expect(noisyThresholds({}, fb)).toEqual(fb)
    expect(noisyThresholds(null, fb)).toEqual(fb)
  })
})
