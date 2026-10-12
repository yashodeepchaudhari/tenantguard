import { config } from '../config.ts'
import type { StateSnapshot } from '../types.ts'

export interface NoisyThresholds {
  warn: number
  throttle: number
  quarantine: number
}

const ok = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v) && v > 0

/** Brain-provided StateSnapshot.noisy_thresholds win; the VITE_NOISY_* env copies are only a fallback. */
export function noisyThresholds(snap: Pick<StateSnapshot, 'noisy_thresholds'> | null, fallback: NoisyThresholds = config.noisy): NoisyThresholds {
  const t = snap?.noisy_thresholds ?? {}
  return {
    warn: ok(t.warn) ? t.warn : fallback.warn,
    throttle: ok(t.throttle) ? t.throttle : fallback.throttle,
    quarantine: ok(t.quarantine) ? t.quarantine : fallback.quarantine,
  }
}
