// Jury challenge: a judge secretly turns one company into the attacker; TenantGuard must name it from the
// server measurements alone. The pick is sealed (sha256 of tenant|attack|salt) before the hunt and opened after.
import type { IncidentEvent, Level, StateSnapshot, TenantView } from '../types.ts'
import { LEVELS } from '../types.ts'

export type ChallengeKind = 'whale' | 'flood' | 'blackbox_bomb' | 'honest_spike' | 'faulty_server'

export interface KindInfo {
  kind: ChallengeKind
  label: string
  about: string
  /** true: a company is to blame (the judge's pick). false: the right answer is to blame nobody. */
  blame: boolean
  target: 'tenant' | 'server'
  intensity: number
}

export const CHALLENGE_KINDS: KindInfo[] = [
  {
    kind: 'whale',
    label: 'Heavy searches',
    about: 'a few very expensive queries',
    blame: true,
    target: 'tenant',
    intensity: 2,
  },
  {
    kind: 'flood',
    label: 'Flood',
    about: '10× its normal number of calls',
    blame: true,
    target: 'tenant',
    intensity: 2,
  },
  {
    kind: 'blackbox_bomb',
    label: 'Hidden work',
    about: 'cheap calls, heavy hidden CPU: a rate limiter lets it through',
    blame: true,
    target: 'tenant',
    intensity: 2,
  },
  {
    kind: 'honest_spike',
    label: 'Honest growth',
    about: '3× its own normal traffic, within its plan: nobody to blame',
    blame: false,
    target: 'tenant',
    intensity: 1,
  },
  {
    kind: 'faulty_server',
    label: 'Broken server',
    about: 'a server turns slow by itself: no company to blame',
    blame: false,
    target: 'server',
    intensity: 1,
  },
]

export const kindInfo = (k: ChallengeKind) => CHALLENGE_KINDS.find((x) => x.kind === k) as KindInfo

/** Seconds TenantGuard gets before the seal is opened anyway (the same for every kind, so it gives nothing away). */
export const HUNT_LIMIT_S = 45

// ---------- sha256 (works on plain http too, where crypto.subtle is missing) ----------
const K = new Uint32Array([
  0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5, 0xd807aa98, 0x12835b01,
  0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174, 0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc,
  0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da, 0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147,
  0x06ca6351, 0x14292967, 0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
  0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070, 0x19a4c116, 0x1e376c08,
  0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3, 0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208,
  0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
])

export function sha256(text: string): string {
  const msg = new TextEncoder().encode(text)
  const len = Math.ceil((msg.length + 9) / 64) * 64
  const buf = new Uint8Array(len)
  buf.set(msg)
  buf[msg.length] = 0x80
  const view = new DataView(buf.buffer)
  view.setUint32(len - 4, msg.length * 8)
  view.setUint32(len - 8, Math.floor((msg.length * 8) / 2 ** 32))
  const h = new Uint32Array([0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19])
  const w = new Uint32Array(64)
  const rotr = (x: number, n: number) => (x >>> n) | (x << (32 - n))
  for (let off = 0; off < len; off += 64) {
    for (let i = 0; i < 16; i++) w[i] = view.getUint32(off + i * 4)
    for (let i = 16; i < 64; i++) {
      const s0 = rotr(w[i - 15], 7) ^ rotr(w[i - 15], 18) ^ (w[i - 15] >>> 3)
      const s1 = rotr(w[i - 2], 17) ^ rotr(w[i - 2], 19) ^ (w[i - 2] >>> 10)
      w[i] = (w[i - 16] + s0 + w[i - 7] + s1) >>> 0
    }
    let [a, b, c, d, e, f, g, hh] = h
    for (let i = 0; i < 64; i++) {
      const t1 = (hh + (rotr(e, 6) ^ rotr(e, 11) ^ rotr(e, 25)) + ((e & f) ^ (~e & g)) + K[i] + w[i]) >>> 0
      const t2 = ((rotr(a, 2) ^ rotr(a, 13) ^ rotr(a, 22)) + ((a & b) ^ (a & c) ^ (b & c))) >>> 0
      hh = g
      g = f
      f = e
      e = (d + t1) >>> 0
      d = c
      c = b
      b = a
      a = (t1 + t2) >>> 0
    }
    h[0] += a
    h[1] += b
    h[2] += c
    h[3] += d
    h[4] += e
    h[5] += f
    h[6] += g
    h[7] += hh
  }
  return Array.from(h, (x) => x.toString(16).padStart(8, '0')).join('')
}

export function newSalt(): string {
  const b = crypto.getRandomValues(new Uint8Array(8))
  return Array.from(b, (x) => x.toString(16).padStart(2, '0')).join('')
}

/** What is shown on screen before the hunt: commits to the pick without revealing it. */
export function seal(tenant: string, attack: string, salt: string): string {
  return sha256(`${tenant}|${attack}|${salt}`)
}

// ---------- reading the brain's answer ----------
export const eventKey = (e: IncidentEvent) => `${e.incident_id}|${e.stage}|${e.ts}|${e.level ?? ''}`

const rank = (l: Level | null | undefined) => (l ? LEVELS.indexOf(l) : -1)
const PUNISHING: Level[] = ['throttle', 'deprioritize', 'quarantine', 'blocked']

export interface Answer {
  tenant: string
  stage: IncidentEvent['stage']
  detail: string
  confidence: number | null
}

/** Verdicts that look at a company and clear it: no penalty. */
export const CLEARING = ['honest_growth', 'host_fault', 'mitigated_no_blame']

const isPunishing = (e: IncidentEvent) =>
  (e.stage === 'verdict' && !CLEARING.includes(e.verdict ?? '')) ||
  (e.stage === 'level_change' && PUNISHING.includes(e.level as Level) && rank(e.level) > rank(e.prev_level))

/** The first event that arrived after the hunt started (not in `before`) and points at a tenant.
 *  mode 'name': the decoder naming it, a proof, a blaming verdict, or the ladder escalating it to throttle or worse.
 *  mode 'blame': only what punishes it (a blaming verdict or that escalation); being named and then cleared is fine.
 *  De-escalations of an earlier incident never count. `events` is newest first, as the store keeps them. */
export function findAnswer(events: IncidentEvent[], before: Set<string>, mode: 'name' | 'blame' = 'name'): Answer | null {
  const fresh = events.filter((e) => !before.has(eventKey(e)) && !!e.tenant)
  for (let i = fresh.length - 1; i >= 0; i--) {
    const e = fresh[i]
    const hit = isPunishing(e) || (mode === 'name' && (e.stage === 'named' || e.stage === 'proving'))
    if (!hit) continue
    const s = e.suspects.find((x) => x.tenant === e.tenant)
    return {
      tenant: e.tenant as string,
      stage: e.stage,
      detail: e.detail,
      confidence: s ? s.confidence : null,
    }
  }
  return null
}

/** Companies the brain looked at during the round and cleared (honest growth, host fault, no blame). */
export function cleared(events: IncidentEvent[], before: Set<string>): { tenant: string; verdict: string }[] {
  return events
    .filter((e) => !before.has(eventKey(e)) && e.stage === 'verdict' && !!e.tenant && CLEARING.includes(e.verdict ?? ''))
    .map((e) => ({ tenant: e.tenant as string, verdict: e.verdict as string }))
}

/** Tenants other than the culprit that TenantGuard is holding back right now. */
export function innocentsPunished(tenants: TenantView[], culprit: string): TenantView[] {
  return tenants.filter((t) => t.tenant !== culprit && (PUNISHING.includes(t.level) || t.factor < 1))
}

/** Tenants already held back before a round starts (they would muddy the answer). */
export function stillHot(tenants: TenantView[]): TenantView[] {
  return tenants.filter((t) => PUNISHING.includes(t.level))
}

export interface Outcome {
  right: boolean
  title: string
}

/** Blame kinds: right = the judge's company was named. Nobody kinds: right = no company was blamed. */
export function outcome(kind: ChallengeKind, target: string, answer: Answer | null): Outcome {
  if (kindInfo(kind).blame) {
    if (!answer) return { right: false, title: '✕ Not found' }
    return answer.tenant === target ? { right: true, title: '✓ Found it' } : { right: false, title: '✕ Wrong company' }
  }
  return answer
    ? {
        right: false,
        title: `✕ Blamed ${answer.tenant}, but nobody was to blame`,
      }
    : { right: true, title: '✓ Blamed nobody' }
}

export interface Evidence {
  tenant: string
  score: number // brain noisy score
  warn: number // flag threshold
  cpuShare: number // 0..1 of all tenants' CPU
  neighbours: number // tenants sharing a server with it
  neighboursOver: number // of those, p95 over the SLO
  sloMs: number
}

/** The numbers behind a name, read from one snapshot. */
export function evidence(
  snap: StateSnapshot | null,
  barcodes: Record<string, string[]>,
  tenant: string,
  sloMs: number,
  warnFallback: number,
): Evidence | null {
  const t = snap?.tenants.find((x) => x.tenant === tenant)
  if (!snap || !t) return null
  const mine = new Set(barcodes[tenant] ?? [])
  const nbrs = snap.tenants.filter((o) => o.tenant !== tenant && (barcodes[o.tenant] ?? []).some((sv) => mine.has(sv)))
  return {
    tenant,
    score: t.noisy_score,
    warn: snap.noisy_thresholds?.warn ?? warnFallback,
    cpuShare: t.cpu_share,
    neighbours: nbrs.length,
    neighboursOver: nbrs.filter((o) => o.p95_ms >= sloMs).length,
    sloMs,
  }
}

/** The company with the highest noisy score (what TenantGuard saw, even when it rightly blamed nobody). */
export function busiest(tenants: TenantView[]): TenantView | null {
  return tenants.reduce<TenantView | null>((best, t) => (!best || t.noisy_score > best.noisy_score ? t : best), null)
}
