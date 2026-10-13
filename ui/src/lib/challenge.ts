// Jury challenge: a judge secretly picks what happens (a company attacks, or nobody is to blame); TenantGuard must
// name the culprit or blame nobody, from the server measurements alone. The choice is sealed (sha256 of
// target|kind|salt) and shown in full before the round, so the judges can check it themselves afterwards.
import type { IncidentEvent, StateSnapshot, TenantView } from '../types.ts'
import { ACTED } from './impact.ts'
import { FLEET } from './status.ts'
import { eventKey } from './store.ts'

export { eventKey }

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
  { kind: 'whale', label: 'Heavy searches', about: 'a few very expensive queries', blame: true, target: 'tenant', intensity: 2 },
  { kind: 'flood', label: 'Flood', about: '10× its normal number of calls', blame: true, target: 'tenant', intensity: 2 },
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
/** Verdicts that look at a company (or a server) and clear it: no penalty. */
export const CLEARING = ['honest_growth', 'host_fault', 'mitigated_no_blame']
const CLOSED: IncidentEvent['stage'][] = ['resolved', 'explained']
/** An incident with no event for this long (brain clock) no longer blocks a new round. */
export const INCIDENT_QUIET_S = 60

/** Where a round starts: the incident events already seen, and the brain's clock (StateSnapshot.ts) at that moment. */
export interface RoundStart {
  before: Set<string>
  brainTs: number
}

/** Events of this round: not seen before it started, and stamped by the brain at or after its start (a late
 *  backfill of old events, or an earlier incident still finishing its proof, is ignored). Newest first. */
export function roundEvents(events: IncidentEvent[], start: RoundStart): IncidentEvent[] {
  return events.filter((e) => !start.before.has(eventKey(e)) && e.ts >= start.brainTs - 1)
}

const isPunishing = (e: IncidentEvent) =>
  (e.stage === 'verdict' && !!e.verdict && !CLEARING.includes(e.verdict)) ||
  (e.stage === 'level_change' && !!e.level && ACTED.includes(e.level) && !(e.prev_level && ACTED.includes(e.prev_level)))

export interface Answer {
  tenant: string
  stage: IncidentEvent['stage']
  detail: string
  confidence: number | null
  /** other companies named in the same event (the decoder can name several culprits; `tenant` is only the first) */
  also: string[]
}

/** The first event of the round that points at a company.
 *  mode 'name': the decoder naming it, a proof, a blaming verdict, or the ladder acting on it (throttle or worse).
 *  mode 'blame': only what punishes it (a blaming verdict or that ladder step); named and then cleared is fine. */
export function findAnswer(events: IncidentEvent[], start: RoundStart, mode: 'name' | 'blame' = 'name'): Answer | null {
  const mine = roundEvents(events, start).filter((e) => !!e.tenant)
  for (let i = mine.length - 1; i >= 0; i--) {
    const e = mine[i]
    const hit = isPunishing(e) || (mode === 'name' && (e.stage === 'named' || e.stage === 'proving'))
    if (!hit) continue
    const s = e.suspects.find((x) => x.tenant === e.tenant)
    const also =
      e.stage === 'named' ? e.suspects.filter((x) => x.tenant !== e.tenant && x.confidence >= 0.5).map((x) => x.tenant) : []
    return { tenant: e.tenant as string, stage: e.stage, detail: e.detail, confidence: s ? s.confidence : null, also }
  }
  return null
}

/** Companies the brain looked at during the round and cleared (honest growth, no blame). */
export function cleared(events: IncidentEvent[], start: RoundStart): { tenant: string; verdict: string }[] {
  return roundEvents(events, start)
    .filter((e) => e.stage === 'verdict' && !!e.tenant && CLEARING.includes(e.verdict ?? ''))
    .map((e) => ({ tenant: e.tenant as string, verdict: e.verdict as string }))
}

/** Servers the brain called a host fault during the round (those verdicts carry a server, no tenant). */
export function hostFaults(events: IncidentEvent[], start: RoundStart): string[] {
  const out = roundEvents(events, start)
    .filter((e) => e.stage === 'verdict' && e.verdict === 'host_fault')
    .map((e) => (typeof e.report?.server === 'string' ? e.report.server : '?'))
  return [...new Set(out)]
}

/** Incidents still open: newest event not resolved/explained and less than INCIDENT_QUIET_S old (brain clock). */
export function openIncidents(events: IncidentEvent[], brainNow: number): string[] {
  const latest = new Map<string, IncidentEvent>()
  for (const e of events) if (!latest.has(e.incident_id)) latest.set(e.incident_id, e) // newest first
  return [...latest.values()]
    .filter((e) => !CLOSED.includes(e.stage) && brainNow - e.ts < INCIDENT_QUIET_S)
    .map((e) => e.incident_id)
}

/** Held back by TenantGuard: a ladder action, a throttle factor, or moved to the quarantine lane. */
export const isHeldBack = (t: TenantView) => ACTED.includes(t.level) || t.factor < 1 || t.placement === 'quarantine'

/** Companies other than `culprit` that TenantGuard is holding back ('' = everyone counts as innocent). */
export function innocentsPunished(tenants: TenantView[], culprit: string): TenantView[] {
  return tenants.filter((t) => t.tenant !== culprit && isHeldBack(t))
}

/** Companies not back to normal yet (held back, or moved off their own servers for a proof): no round until calm. */
export function stillHot(tenants: TenantView[]): TenantView[] {
  return tenants.filter((t) => isHeldBack(t) || t.placement !== 'normal')
}

/** The 9 fleet servers marked slow (not the spare or the quarantine worker). */
export function slowServers(snap: StateSnapshot | null): string[] {
  return (snap?.servers ?? []).filter((s) => FLEET.includes(s.server) && s.status === 'slow').map((s) => s.server)
}

export interface Outcome {
  right: boolean
  title: string
}

/** Blame rounds: right = the judge's company is named first and no other company is held back.
 *  Nobody rounds: right = no company blamed and none held back. `heldBack` = companies held back when the seal opens. */
export function outcome(kind: ChallengeKind, target: string, answer: Answer | null, heldBack: string[]): Outcome {
  if (kindInfo(kind).blame) {
    if (!answer) return { right: false, title: '✕ Not found' }
    if (answer.tenant !== target) {
      return answer.also.includes(target)
        ? { right: false, title: `✕ Named ${answer.tenant} first (${target} too)` }
        : { right: false, title: '✕ Wrong company' }
    }
    const others = heldBack.filter((t) => t !== target)
    return others.length
      ? { right: false, title: `✕ Found it, but also held back ${others.join(', ')}` }
      : { right: true, title: '✓ Found it' }
  }
  if (answer) return { right: false, title: `✕ Blamed ${answer.tenant}, but nobody was to blame` }
  if (heldBack.length) return { right: false, title: `✕ Held back ${heldBack.join(', ')}, but nobody was to blame` }
  return { right: true, title: '✓ Blamed nobody' }
}

export interface Evidence {
  tenant: string
  score: number // brain noisy score
  warn: number // flag threshold
  cpuShare: number // 0..1 of all tenants' CPU
  neighbours: number // tenants sharing a server with it
  neighboursOver: number // of those, p95 over the SLO
  sloMs: number
  slow: string[] // fleet servers slow at that moment
  exact: boolean // its barcode servers are exactly the slow ones
}

const sameSet = (a: string[], b: string[]) => a.length === b.length && a.every((x) => b.includes(x))

/** The numbers behind a name, read from one snapshot (frozen when the company is named). */
export function evidence(
  snap: StateSnapshot | null,
  barcodes: Record<string, string[]>,
  tenant: string,
  sloMs: number,
  warnFallback: number,
): Evidence | null {
  const t = snap?.tenants.find((x) => x.tenant === tenant)
  if (!snap || !t) return null
  const mine = barcodes[tenant] ?? []
  const nbrs = snap.tenants.filter((o) => o.tenant !== tenant && (barcodes[o.tenant] ?? []).some((sv) => mine.includes(sv)))
  const slow = slowServers(snap)
  return {
    tenant,
    score: t.noisy_score,
    warn: snap.noisy_thresholds?.warn ?? warnFallback,
    cpuShare: t.cpu_share,
    neighbours: nbrs.length,
    neighboursOver: nbrs.filter((o) => o.p95_ms >= sloMs).length,
    sloMs,
    slow,
    exact: mine.length > 0 && sameSet(mine, slow),
  }
}

/** The company with the highest noisy score (what TenantGuard saw, even when it rightly blamed nobody). */
export function busiest(tenants: TenantView[]): TenantView | null {
  return tenants.reduce<TenantView | null>((best, t) => (!best || t.noisy_score > best.noisy_score ? t : best), null)
}
