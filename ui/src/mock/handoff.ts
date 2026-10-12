// Mock handoff bridge: derives CaseView records (contracts §6) from the mock incident stream,
// the way the real bridge turns brain incidents into Web PS3 cases. Pure, so the Node mock
// server can serve the same thing.
import { summarizeCase } from '../lib/case.ts'
import type { CaseAction, CaseView, IncidentEvent, Verdict } from '../types.ts'

const DECISION_WINDOW_S = 120 // tenantguard.yaml handoff.decision_window_s
const OWNER = 'on-call SRE (rota 2)'

const ist = (ts: number) =>
  new Date((ts + 5.5 * 3600) * 1000).toISOString().slice(11, 19) // HH:MM:SS in IST, display only

export function mockCases(events: IncidentEvent[], now: number, uiOrigin = 'http://127.0.0.1:5173'): CaseView[] {
  const ids = [...new Set(events.map((e) => e.incident_id))]
  const out: CaseView[] = []
  let n = 0
  for (const id of ids) {
    const c = summarizeCase(id, events)
    // the bridge opens a case once TenantGuard acts (throttle or a verdict), not on a plain warn
    const acted = c.events.find((e) => e.stage === 'verdict' || (e.stage === 'level_change' && e.level && e.level !== 'warn' && e.level !== 'normal'))
    if (!acted || c.openedAt === null) continue
    n += 1
    const created = acted.ts
    const verdict: Verdict = (c.verdict as Verdict) ?? 'costly_pattern'
    const ownerAt = created + 8
    const deadline = created + DECISION_WINDOW_S
    const closed = c.closed
    const action: CaseAction = {
      action_id: 'A1',
      text: `Keep ${c.tenant} throttled and block it for 300 s if it does not calm down`,
      deadline_at: deadline,
      deadline_ist: ist(deadline),
      state: closed ? 'resolved' : now >= deadline ? 'expired' : 'pending',
      decided_by: closed ? 'TenantGuard (auto-recovered)' : null,
      decided_at: closed ? c.lastAt : null,
    }
    const history = [
      { at: created, time_ist: ist(created), event: `Case opened from ${id} (${verdict})` },
      ...(now >= ownerAt ? [{ at: ownerAt, time_ist: ist(ownerAt), event: `Owner set: ${OWNER}` }] : []),
      ...(closed && c.lastAt ? [{ at: c.lastAt, time_ist: ist(c.lastAt), event: 'Incident resolved in TenantGuard' }] : []),
    ]
    out.push({
      case_id: `INC-${String(n).padStart(4, '0')}`,
      incident_id: id,
      tenant: c.tenant,
      title: `${c.tenant ?? 'A tenant'} is crowding its neighbours`,
      severity: c.events.some((e) => e.level === 'quarantine') ? 'high' : 'medium',
      status: closed ? 'resolved' : now >= ownerAt ? 'in_progress' : 'waiting_for_decision',
      context: {
        verdict,
        affected: [],
        evidence_link: `${uiOrigin}/case/${encodeURIComponent(id)}`,
        summary: c.detail,
      },
      actions: [action],
      history,
      owner: now >= ownerAt ? OWNER : null,
      owner_since_at: now >= ownerAt ? ownerAt : null,
      owner_since_ist: now >= ownerAt ? ist(ownerAt) : null,
      created_at: created,
      updated_at: history[history.length - 1].at,
      delivered_at: created + 0.4,
    })
  }
  return out
}
