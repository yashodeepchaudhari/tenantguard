// Runs the mock platform headless through calm -> whale on K -> calm in each mode.
// Usage: node scripts/sim-smoke.mjs   (Node >= 23.6 runs the .ts import natively)
import { MockPlatform } from '../src/mock/sim.ts'

for (const mode of ['off', 'industry', 'tenantguard']) {
  const p = new MockPlatform()
  p.setMode(mode)
  let t = 1_000_000
  const lines = []
  for (let i = 0; i < 200; i++) {
    t += 0.5
    if (i === 20) p.startAttack({ attack: 'whale', tenant: 'K', intensity: 2, duration_s: 40 }, t)
    const { snapshot, incidents } = p.tick(t)
    for (const e of incidents) lines.push(`  t=${(i / 2).toFixed(1)}s ${e.stage} ${e.prev_level ?? ''}->${e.level ?? ''} ${e.detail}`)
    if (i % 20 === 19) {
      const k = snapshot.tenants.find((x) => x.tenant === 'K')
      const a = snapshot.tenants.find((x) => x.tenant === 'E') // K's project peer
      lines.push(
        `t=${(i / 2).toFixed(0)}s K lvl=${k.level} f=${k.factor.toFixed(2)} score=${k.noisy_score.toFixed(1)} cpu=${(k.cpu_share * 100).toFixed(0)}% | ` +
          `E (peer) p95=${a.p95_ms.toFixed(0)}ms | util=${(snapshot.utilization * 100).toFixed(0)}% fair=${snapshot.fairness.toFixed(2)} | ${snapshot.status_sentence}`,
      )
    }
  }
  console.log(`=== ${mode}\n${lines.join('\n')}`)
}
