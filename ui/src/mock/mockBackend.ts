// In-browser fixture replay: the MockPlatform simulation behind the same Backend interface.
// Enabled with VITE_MOCK=1 (npm run dev:mock) or ?mock=1.
import { ApiError, type Backend, type ConnState } from '../lib/backend.ts'
import type { BrainMessage, IncidentEvent } from '../types.ts'
import { MockPlatform, SimError } from './sim.ts'

const TICK_MS = 500 // brain broadcasts StateSnapshot at 2 Hz
const now = () => Date.now() / 1000

export function createMockBackend(opts: { script?: boolean } = {}): Backend {
  const sim = new MockPlatform()
  const listeners = new Set<(m: BrainMessage) => void>()
  const history: IncidentEvent[] = []
  let timer: ReturnType<typeof setInterval> | undefined
  const started = now()
  let scripted = !(opts.script ?? true)

  const step = () => {
    // Scripted fixture: a whale on K a few seconds in, so the screen shows the ladder.
    if (!scripted && now() - started > 4) {
      scripted = true
      sim.startAttack({ attack: 'whale', tenant: 'K', intensity: 2, duration_s: 45 }, now())
    }
    const { snapshot, incidents } = sim.tick(now())
    history.push(...incidents)
    if (history.length > 500) history.splice(0, history.length - 500)
    for (const l of listeners) {
      for (const e of incidents) l(e)
      l(snapshot)
    }
  }

  const call = async <T>(fn: () => T): Promise<T> => {
    await new Promise((r) => setTimeout(r, 60)) // feel like a network call
    try {
      return structuredClone(fn())
    } catch (e) {
      if (e instanceof SimError) throw new ApiError(e.status, `${e.status} ${e.message}`)
      throw e
    }
  }

  return {
    kind: 'mock',
    subscribe(onMessage, onConn: (s: ConnState) => void) {
      listeners.add(onMessage)
      onConn('open')
      if (!timer) {
        // warm up so charts have a short history immediately
        timer = setInterval(step, TICK_MS)
      }
      step()
      return () => {
        listeners.delete(onMessage)
        if (listeners.size === 0) {
          clearInterval(timer)
          timer = undefined
        }
        onConn('closed')
      }
    },
    incidents: () => call(() => history.slice()),
    getMode: () => call(() => sim.mode),
    setMode: (mode) => call(() => sim.setMode(mode).mode),
    tenants: () => call(() => sim.tenantsAdmin()),
    patchQuota: (id, patch) => call(() => sim.patchQuota(id, patch)),
    overrides: () => call(() => sim.overrides(now())),
    labStatus: () => call(() => sim.labStatus(now())),
    startAttack: (req) => call(() => sim.startAttack(req, now())),
    stopAttack: (id) => call(() => sim.stopAttack(id, now())),
  }
}
