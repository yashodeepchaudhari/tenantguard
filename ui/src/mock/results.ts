// FIXTURE RunResults for developing the Scorecard before the lab exists.
// These numbers are invented: they are NOT measurements and must never reach a slide
// (PLAN §2.12: numbers on slides come only from lab/results/*.json). The page shows a
// "fixture data" banner whenever this list is in use.
import type { AttackName, Mode, PhaseP99, RunResult } from '../types.ts'

const VICTIMS = ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H', 'I', 'J', 'L']

interface Shape {
  scenario: AttackName
  attacker: string | null
  // victim p99 during the attack, per mode (ms); baseline ~ 30 ms
  attack: Record<Mode, number>
  after: Record<Mode, number>
  detect: Record<Mode, number | null>
  recover: Record<Mode, number | null>
  a429: Record<Mode, number>
}

const SHAPES: Shape[] = [
  {
    scenario: 'whale',
    attacker: 'K',
    attack: { off: 1480, industry: 240, tenantguard: 62 },
    after: { off: 41, industry: 33, tenantguard: 31 },
    detect: { off: null, industry: null, tenantguard: 3.5 },
    recover: { off: null, industry: 28, tenantguard: 9.5 },
    a429: { off: 0, industry: 1830, tenantguard: 2610 },
  },
  {
    scenario: 'flood',
    attacker: 'B',
    attack: { off: 860, industry: 72, tenantguard: 58 },
    after: { off: 36, industry: 32, tenantguard: 31 },
    detect: { off: null, industry: null, tenantguard: 2.5 },
    recover: { off: null, industry: 6, tenantguard: 5 },
    a429: { off: 0, industry: 9120, tenantguard: 9640 },
  },
  {
    scenario: 'retry_storm',
    attacker: 'G',
    attack: { off: 1210, industry: 310, tenantguard: 88 },
    after: { off: 52, industry: 39, tenantguard: 32 },
    detect: { off: null, industry: null, tenantguard: 4 },
    recover: { off: null, industry: null, tenantguard: 12 },
    a429: { off: 0, industry: 14200, tenantguard: 6100 },
  },
  {
    scenario: 'blackbox_bomb',
    attacker: 'K',
    attack: { off: 1350, industry: 1290, tenantguard: 95 },
    after: { off: 44, industry: 47, tenantguard: 33 },
    detect: { off: null, industry: null, tenantguard: 6.5 },
    recover: { off: null, industry: null, tenantguard: 17 },
    a429: { off: 0, industry: 0, tenantguard: 0 },
  },
]

const MODES: Mode[] = ['off', 'industry', 'tenantguard']
const T0 = 1791800000

function phase(base: number, attack: number, after: number, j: number): PhaseP99 {
  return { baseline: base + j, attack: attack * (1 + j / 50), after: after + j / 2 }
}

export const RESULTS_FIXTURE: RunResult[] = SHAPES.flatMap((s, si) =>
  MODES.flatMap((mode, mi) =>
    [0, 1].map((rep) => {
      const j = ((si * 7 + mi * 3 + rep * 5) % 5) - 2 // small deterministic jitter
      const started = T0 + si * 3600 + mi * 600 + rep * 120
      const byTenant: Record<string, PhaseP99> = {}
      VICTIMS.filter((v) => v !== s.attacker).forEach((v, vi) => {
        byTenant[v] = phase(28 + (vi % 4), s.attack[mode] * (0.6 + (vi % 5) / 10), s.after[mode], j)
      })
      const scenarioMode = `${s.scenario}-${mode}`
      return {
        run_id: `fixture-${scenarioMode}-${rep + 1}`,
        scenario: s.scenario,
        mode,
        attacker: s.attacker,
        started_at: started,
        victim_p99_ms: phase(30, s.attack[mode], s.after[mode], j),
        victim_p99_by_tenant: byTenant,
        time_to_detect_s: s.detect[mode] === null ? null : s.detect[mode]! + rep * 0.5,
        time_to_recover_s: s.recover[mode] === null ? null : s.recover[mode]! + j,
        attacker_429: s.a429[mode] ? s.a429[mode] + rep * 37 : 0,
        attacker_503: mode === 'off' ? 0 : 40 + j,
        file: `lab/results/FIXTURE-${scenarioMode}-${rep + 1}.json`,
      }
    }),
  ),
)
