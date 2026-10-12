// Mirror of common/tenantguard_common/contracts.py (v2). Keep field names, optionality and
// comments in sync; the Python file is the source of truth.
//
// Units (apply everywhere unless a field says otherwise):
// - every `ts`, `*_at` field: unix epoch seconds as float
// - every `*_ms` field: milliseconds as float
// - shares / ratios marked 0..1 are fractions, not percents

export type Mode = 'off' | 'industry' | 'tenantguard'
export type Tier = 'enterprise' | 'pro' | 'free'
export type Level = 'normal' | 'warn' | 'throttle' | 'deprioritize' | 'quarantine' | 'blocked'

export const MODES: Mode[] = ['off', 'industry', 'tenantguard']
export const LEVELS: Level[] = ['normal', 'warn', 'throttle', 'deprioritize', 'quarantine', 'blocked']

// ---------- 0. Tenancy / admin (gateway /admin/*) ----------
export interface Quota {
  rate_rps: number // token bucket refill, requests/s
  burst: number // token bucket capacity, requests
  cost_ms_per_s: number // cost bucket refill, CPU-ms per second
  concurrency: number // max in-flight requests
}

/** Output of GET /admin/tenants (list) and GET /admin/tenants/{id}. */
export interface TenantAdminView {
  id: string
  name: string
  tier: Tier
  servers: string[]
  weight: number
  quota: Quota // configured quota (after PATCH)
  effective_quota: Quota // quota after the active LimitsCmd factor
  factor: number // default 1.0
  weight_factor: number // default 1.0
  blocked: boolean
  placement: string // normal | spare | quarantine | wN
  override_expires_at?: number | null
  monthly_revenue_rs: number
  protected: boolean
}

export interface QuotaPatch {
  rate_rps?: number | null
  burst?: number | null
  cost_ms_per_s?: number | null
  concurrency?: number | null
}

export interface ModeBody {
  mode: Mode
}

// ---------- 3. Control (subset the UI reads via GET /admin/overrides) ----------
export interface ControlBase {
  incident_id?: string | null
  ttl_s: number
}

export interface LimitsCmd extends ControlBase {
  tenant: string
  factor: number
  weight_factor: number
}

export interface BlockCmd extends ControlBase {
  tenant: string
  blocked: boolean
}

export interface RelocateCmd extends ControlBase {
  tenant: string
  cls?: string | null
  target?: string | null
}

export interface DrainCmd extends ControlBase {
  server: string
  drained: boolean
}

/** Output of gateway GET /admin/overrides. */
export interface OverridesView {
  limits: Record<string, LimitsCmd>
  blocks: Record<string, BlockCmd>
  relocations: RelocateCmd[]
  route_weights: Record<string, Record<string, number>>
  drains: Record<string, DrainCmd>
  expires_at: Record<string, number> // "<kind>:<key>" -> epoch s
  last_heartbeat_age_s?: number | null
  fail_static: boolean
}

// ---------- 4. Brain -> UI (WS /ws/events on the brain) ----------
export interface TenantView {
  tenant: string
  name: string
  tier: Tier
  p95_ms: number // end-to-end p95 over the last 5 s
  rps: number // completed requests/s, last 5 s
  rejected_rps: number // 429 + 503 per second, last 5 s
  cpu_share: number // 0..1 share of all tenants' CPU, last 5 s
  noisy_score: number // raw ratio >= 0 (warn 1.5 ...)
  noisy_norm: number // 0..1, for bars
  level: Level
  factor: number // current throttle factor applied
  placement: string // normal | spare | quarantine | wN
}

export interface ServerView {
  server: string
  p95_ms: number // server-side p95 over the last 2 s
  peer_ratio: number
  cpu_pct: number // 0..100 of the container CPU limit
  status: 'healthy' | 'slow' | 'drained'
}

export interface StateSnapshot {
  type: 'state'
  ts: number
  mode: Mode
  tenants: TenantView[]
  servers: ServerView[]
  utilization: number // 0..1 of fleet CPU limit
  fairness: number // Jain index 0..1
  status_sentence: string
  brain_ok: boolean
  /** e.g. {warn: 1.5, throttle: 2.0, quarantine: 4.0} from brain.noisy; the UI uses these, not its own copies */
  noisy_thresholds?: Record<string, number>
}

export type Stage =
  | 'detected'
  | 'level_change'
  | 'rescued'
  | 'named'
  | 'proving'
  | 'verdict'
  | 'resolved'
  | 'explained'
  | 'closed'

export interface Suspect {
  tenant: string
  confidence: number // 0..1
}

export interface IncidentEvent {
  type: 'incident'
  incident_id: string
  stage: Stage
  ts: number
  tenant?: string | null
  level?: Level | null
  prev_level?: Level | null
  suspects: Suspect[]
  verdict?: string | null
  action?: string | null
  detail: string
  report?: Record<string, unknown> | null
}

export type BrainMessage = StateSnapshot | IncidentEvent

// ---------- 5. Lab (lab service :8300) ----------
export type AttackName =
  | 'flood'
  | 'whale'
  | 'hog'
  | 'memory'
  | 'retry_storm'
  | 'background_bomb'
  | 'blackbox_bomb'
  | 'cache_flood'
  | 'faulty_server'
  | 'honest_spike'

export const ATTACKS: AttackName[] = [
  'flood',
  'whale',
  'hog',
  'memory',
  'retry_storm',
  'background_bomb',
  'blackbox_bomb',
  'cache_flood',
  'faulty_server',
  'honest_spike',
]

/** Valid intensity range for AttackReq / RunReq (lab returns 422 outside). */
export const INTENSITY_MIN = 0.1
export const INTENSITY_MAX = 5
export const DURATION_MAX_S = 600

/** POST /lab/attack */
export interface AttackReq {
  attack: AttackName
  tenant?: string | null // faulty_server uses server instead
  server?: string | null
  intensity: number // 0.1..5
  duration_s: number // (0, 600]
}

export interface AttackStatus {
  attack_id: string
  attack: AttackName
  tenant?: string | null
  server?: string | null
  intensity: number
  state: 'running' | 'stopped' | 'done'
  started_at: number
  ends_at: number
}

/** POST /lab/run: calm -> attack -> calm, after setting the gateway mode. */
export interface RunReq {
  scenario: AttackName
  mode: Mode
  tenant?: string | null
  server?: string | null
  intensity: number // 0.1..5
  calm_s: number // default 20
  attack_s: number // default 40
}

export interface PhaseP99 {
  baseline: number
  attack: number
  after: number
}

/** Written to lab/results/<ts>-<scenario>-<mode>.json. Slide numbers come only from these. */
export interface RunResult {
  run_id: string
  scenario: AttackName
  mode: Mode
  attacker?: string | null
  started_at: number
  victim_p99_ms: PhaseP99 // pooled over all non-attacker tenants
  victim_p99_by_tenant: Record<string, PhaseP99>
  time_to_detect_s?: number | null // attack start -> first incident event
  time_to_recover_s?: number | null // attack start -> victim p99 <= 1.5x baseline for 5 s
  attacker_429: number
  attacker_503: number
  file: string
}

/** GET /lab/status */
export interface LabStatus {
  attacks: AttackStatus[]
  calm_running: boolean
  run?: string | null
}

// ---------- 6. Handoff cases (bridge integrations/handoff on :8400) ----------
export type Verdict = 'costly_pattern' | 'over_plan' | 'honest_growth' | 'host_fault' | 'mitigated_no_blame'
export type Severity = 'low' | 'medium' | 'high'
export type CaseStatus = 'waiting_for_decision' | 'in_progress' | 'resolved' | 'closed'
export type CaseDecision = 'approve' | 'reject' | 'extend' | 'release' | 'resolve'
export type ActionState = 'pending' | 'approved' | 'rejected' | 'extended' | 'released' | 'resolved' | 'expired'

export interface CaseContext {
  verdict: Verdict
  affected: string[] // victim tenant ids (barcode neighbours), may be empty
  evidence_link: string // http://<ui-host>:5173/case/<incident_id>
  summary: string
}

export interface CaseAction {
  action_id: string // "A1", "A2", ... unique within a case
  text: string
  deadline_at: number // epoch s; after it TenantGuard applies its safe default (TTL auto-undo)
  deadline_ist: string // display only, "HH:MM:SS" IST
  state: ActionState
  decided_by?: string | null
  decided_at?: number | null
}

export interface CaseHistoryItem {
  at: number // epoch s
  time_ist: string // display only
  event: string
}

/** Bridge GET /cases (list) and GET /cases/{case_id}. */
export interface CaseView {
  case_id: string // "INC-0001", assigned by the bridge
  incident_id: string // brain IncidentEvent.incident_id
  tenant?: string | null
  title: string
  severity: Severity
  status: CaseStatus
  context: CaseContext
  actions: CaseAction[]
  history: CaseHistoryItem[]
  owner?: string | null
  owner_since_at?: number | null
  owner_since_ist?: string | null
  created_at: number
  updated_at: number
  delivered_at?: number | null // last successful POST to the Web app; null = not yet delivered
}

/** Human decision -> brain POST /api/operator (control token required). */
export interface OperatorCmd {
  tenant: string
  decision: 'release' | 'extend' | 'resume'
  until_s: number
  by: string
  case_id?: string | null
}
