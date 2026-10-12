"""Shared message contracts between gateway, workers, brain, lab and UI.

Owned by the coordinator. Every cross-service JSON message is one of these models.
Change only through the coordinator (see docs/PLAN.md §3).

Units (apply everywhere unless a field says otherwise):
- every `ts`, `*_at` field: unix epoch seconds as float, from time.time() (never monotonic)
- every `*_ms` field: milliseconds as float
- shares / ratios marked 0..1 are fractions, not percents
"""
from __future__ import annotations

from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, Field

Mode = Literal["off", "industry", "tenantguard"]
Tier = Literal["enterprise", "pro", "free"]
Level = Literal["normal", "warn", "throttle", "deprioritize", "quarantine", "blocked"]

# Headers set by the gateway on every forwarded request (workers trust only these).
H_TENANT = "X-TenantGuard-Tenant"
H_CLASS = "X-TenantGuard-Class"          # request class hash computed by gateway (8 hex chars)
H_REQUEST_ID = "X-Request-Id"
# Header set by the worker on every response: handler CPU ms spent on this request,
# as a decimal float string, e.g. "12.375". Background-job CPU is NOT included (Wave 1).
H_CPU_MS = "X-TenantGuard-Cpu-Ms"
# Gateway -> worker: the replica id the request was routed to (v2; replicas are created on demand).
H_REPLICA = "X-TenantGuard-Replica"
# Required on every gateway /admin/* and /control/* request (brain, UI, lab send it).
# Value = env TENANTGUARD_CONTROL_TOKEN (default "dev"); compare with hmac.compare_digest. Missing/wrong -> 401.
H_CONTROL_TOKEN = "X-TenantGuard-Control-Token"


# ---------- 0. Tenancy / admin ----------
class Quota(BaseModel):
    """Per-tenant request limits from the project's tier. CPU is limited per project by the replicas'
    cgroups (v2), so there is no per-tenant CPU bucket."""
    rate_rps: float          # token bucket refill, requests/s
    burst: int               # token bucket capacity, requests
    concurrency: int         # max in-flight requests


class TenantConfig(BaseModel):
    """A tenant as seeded from tenantguard.yaml `tenants` (v2). Carries the demo plaintext key, so it is never
    returned by an API. Tier and quota come from the tenant's project."""
    id: str                  # "A".."L"
    name: str
    project: str             # owning project id (exactly one, invariant I1)
    tier: Tier               # = the project's tier
    api_key: str             # demo plaintext key; only its sha256 leaves the brain
    weight: float = 1.0      # DRR weight inside the project (tenants share a project equally)
    quota: Quota
    monthly_revenue_rs: int = 0
    protected: bool = False
    sla_p95_ms: float = 100.0  # end-to-end p95 promised to this tenant; used for SLA credits


class TenantAdminView(BaseModel):
    """Output of gateway GET /admin/tenants (list) and GET /admin/tenants/{id}."""
    id: str
    name: str
    project: str
    tier: Tier
    replicas: List[str]              # ready replicas of the tenant's project, rendezvous order for this tenant
    weight: float
    quota: Quota
    effective_quota: Quota           # quota after the active LimitsCmd factor
    factor: float = 1.0
    weight_factor: float = 1.0
    blocked: bool = False
    pinned_to: Optional[str] = None  # replica the tenant is pinned to during a split test
    override_expires_at: Optional[float] = None
    monthly_revenue_rs: int = 0
    protected: bool = False


class ModeBody(BaseModel):
    mode: Mode


# ---------- 1. Telemetry (gateway -> brain, WS /internal/telemetry, every tick_ms) ----------
class Cell(BaseModel):
    """Requests of one tenant served by one replica during the last tick (server-side latency, excludes queue wait).
    `server` is the replica id (v2)."""
    tenant: str
    server: str
    count: int
    p50_ms: float
    p95_ms: float
    errors: int = 0


class QueueStat(BaseModel):
    """Per tenant, during the last tick."""
    tenant: str
    depth: int                   # queued now
    wait_p95_ms: float           # gateway queue wait
    p95_ms: float                # end-to-end (queue wait + upstream) p95 of completed requests; 0 if completed==0 (consumers MUST skip such ticks, not treat as fast)
    completed: int
    admitted: int
    rejected_429: int            # refused by admission (rate / cost / concurrency / block)
    expired_503: int             # dropped after bounded wait, or batch shed under saturation
    retries: int = 0             # requests identified as retries (repeated Idempotency-Key, or a repeat of a request that failed with 429/503/5xx in the last retry_budget.window_s)
    inflight: int
    queued_cost_ms: float = 0.0  # sum of the reference cost of the tenant's queued requests now (v2 demand signal)
    refused_cost_ms: float = 0.0 # reference cost of requests refused or expired during the tick (v2 demand signal)


class LimiterStat(BaseModel):
    server: str
    window: int
    inflight: int


class TelemetryFrame(BaseModel):
    ts: float
    mode: Mode
    tick_ms: int = 250
    cells: List[Cell] = Field(default_factory=list)
    queues: List[QueueStat] = Field(default_factory=list)
    limiter: List[LimiterStat] = Field(default_factory=list)


# ---------- 2. Worker stats (worker GET /stats, polled at 1 Hz; counters CUMULATIVE since process start) ----------
class TenantUsage(BaseModel):
    tenant: str
    requests: int
    handler_cpu_ms: float
    bg_cpu_ms: float
    db_ms: float
    cache_hits: int = 0
    cache_misses: int = 0
    evictions_caused: int = 0
    evictions_suffered: int = 0
    bytes_in: int = 0
    mem_mb: float = 0.0          # current (gauge, not cumulative)


class ClassCost(BaseModel):
    tenant: str
    cls: str
    count: int
    cpu_ms_total: float


class WorkerStats(BaseModel):
    server: str
    ts: float
    started_at: float            # process start; a change means counters reset
    cpu_total_ms: float          # whole process tree, cumulative
    blackbox_cpu_ms: float = 0.0 # unattributed "vendor indexer" CPU, cumulative (Wave 2)
    docs_by_tenant: Dict[str, int] = Field(default_factory=dict)   # docs handed to the black box per tenant, cumulative (v2 attribution)
    cpu_limit_cores: float = 0.5 # container CPU limit (worker fills from tenantguard.yaml worker.cpus / env), for cpu_pct
    mem_mb: float
    fault: Optional[dict] = None
    tenants: List[TenantUsage] = Field(default_factory=list)
    classes: List[ClassCost] = Field(default_factory=list)


# ---------- 3. Control (brain -> gateway, POST /control/{kind}) ----------
class ControlBase(BaseModel):
    incident_id: Optional[str] = None
    ttl_s: float = 60.0           # gateway reverts the override after this many seconds


class LimitsCmd(ControlBase):
    """Throttle / deprioritize one tenant. Travels inside the brain's Snapshot (v2).

    - The latest command for a tenant REPLACES the previous one (both factors) and restarts the TTL.
    - Effective int fields = max(1, round(base * factor)); floats = base * factor.
    - factor == 1.0 and weight_factor == 1.0 means no override.
    - Both factors must be in (0, 1].
    """
    tenant: str
    factor: float = 1.0                       # multiplies rate, burst and concurrency
    weight_factor: float = 1.0                # multiplies the tenant's DRR weight inside its project


class BlockCmd(ControlBase):
    """Operator block (handoff approval) -> gateway POST /control/block."""
    tenant: str
    blocked: bool = True                      # blocked=false clears immediately


class ControlAck(BaseModel):
    ok: bool
    detail: str = ""


class OverridesView(BaseModel):
    """Output of gateway GET /admin/overrides."""
    limits: Dict[str, LimitsCmd] = Field(default_factory=dict)        # tenant -> active cmd
    blocks: Dict[str, BlockCmd] = Field(default_factory=dict)
    pins: Dict[str, str] = Field(default_factory=dict)                # tenant -> replica (split test)
    expires_at: Dict[str, float] = Field(default_factory=dict)       # "<kind>:<key>" -> epoch s
    snapshot_version: Optional[int] = None
    last_snapshot_age_s: Optional[float] = None
    fail_static: bool = False


# ---------- 4. Brain -> UI (WS /ws/events on the brain) ----------
class TenantView(BaseModel):
    tenant: str
    name: str
    tier: Tier
    p95_ms: float                 # end-to-end p95 over the last 5 s
    rps: float                    # completed requests/s, averaged over the last 5 s
    rejected_rps: float           # 429 + 503 per second, last 5 s
    cpu_share: float              # 0..1: (handler+bg CPU of tenant) / (handler+bg CPU of all tenants), last 5 s
    noisy_score: float            # raw ratio >= 0; compare with tenantguard.yaml brain.noisy thresholds (warn 1.5 ...)
    noisy_norm: float             # 0..1 = min(1, noisy_score / brain.noisy.quarantine), for bars
    level: Level
    factor: float = 1.0           # current throttle factor applied
    project: str = ""


class ServerView(BaseModel):
    """One replica (v2)."""
    server: str                   # replica id
    p95_ms: float                 # server-side p95 over the last 2 s
    peer_ratio: float = 1.0       # p95 / median p95 of the project's other replicas (1.0 if alone)
    cpu_pct: float                # 0..100 of the replica's CPU cap
    status: Literal["healthy", "slow", "drained"]   # drained = draining before stop
    project: str = ""


class ImpactView(BaseModel):
    """Brain-owned business impact since `since` (UI keeps its own meter as a fallback)."""
    since: float                        # epoch s when counting started (brain start or reset)
    revenue_protected_rs: float         # sum of victims' monthly_revenue_rs x SLO-violation seconds avoided / 2_592_000
    customers_shielded: List[str]       # threatened tenants that stayed within SLO
    slo_violation_avoided_s: float
    slo_violation_suffered_s: float
    innocents_penalised: List[str]      # acted on while their own score < warn; must stay empty


class StateSnapshot(BaseModel):
    type: Literal["state"] = "state"
    ts: float
    mode: Mode
    tenants: List[TenantView]
    servers: List[ServerView]
    utilization: float            # 0..1 of the pool's CPU (v2)
    fairness: float               # Jain index 0..1 over per-tenant (completed rps / weight) of active tenants
    status_sentence: str
    brain_ok: bool = True
    impact: Optional[ImpactView] = None
    noisy_thresholds: Dict[str, float] = Field(default_factory=dict)   # e.g. {"warn":1.5,"throttle":2.0,"quarantine":4.0} from brain.noisy; UI must use these, not its own copies
    pool: Optional["PoolView"] = None   # v2: pool, projects, allocations, replicas


Stage = Literal["detected", "level_change", "rescued", "named", "proving",
                "verdict", "resolved", "explained", "closed"]


class Suspect(BaseModel):
    tenant: str
    confidence: float             # 0..1


class IncidentEvent(BaseModel):
    type: Literal["incident"] = "incident"
    incident_id: str
    stage: Stage
    ts: float
    tenant: Optional[str] = None          # primary tenant of the incident, if any
    level: Optional[Level] = None         # new level (level_change)
    prev_level: Optional[Level] = None
    suspects: List[Suspect] = Field(default_factory=list)
    verdict: Optional[str] = None
    action: Optional[str] = None
    detail: str = ""                      # plain-English reason (every action stores why)
    report: Optional[dict] = None


class FaultReq(BaseModel):
    """Worker POST /admin/fault (header X-Admin-Token). Simulates a faulty host."""
    latency_ms: float = Field(0.0, ge=0, le=10_000)
    cpu_burn_pct: float = Field(0.0, ge=0, le=100)
    duration_s: Optional[float] = Field(None, gt=0, le=3600)   # auto-clear; None = until DELETE


# ---------- 5. Lab (lab service :8300) ----------
AttackName = Literal["flood", "whale", "hog", "memory", "retry_storm", "background_bomb",
                     "blackbox_bomb", "cache_flood", "faulty_server", "honest_spike"]


class AttackReq(BaseModel):
    """POST /lab/attack. intensity = multiplier on the attack's default profile
    (flood: 10x persona rps at 1.0; whale/hog/memory: request rate). Valid 0.1..5, else 422."""
    attack: AttackName
    tenant: Optional[str] = None      # faulty_server uses server instead
    server: Optional[str] = None
    intensity: float = Field(1.0, ge=0.1, le=5.0)
    duration_s: float = Field(60.0, gt=0, le=600)


class AttackStatus(BaseModel):
    attack_id: str
    attack: AttackName
    tenant: Optional[str] = None
    server: Optional[str] = None
    intensity: float = 1.0
    state: Literal["running", "stopped", "done"] = "running"
    started_at: float
    ends_at: float


class LabStatus(BaseModel):
    """GET /lab/status."""
    attacks: List[AttackStatus] = Field(default_factory=list)
    calm_running: bool = False
    run: Optional[str] = None         # id of the scenario run in progress


class RunReq(BaseModel):
    """POST /lab/run: calm -> attack -> calm, after setting the gateway mode."""
    scenario: AttackName
    mode: Mode
    tenant: Optional[str] = None
    server: Optional[str] = None
    intensity: float = Field(1.0, ge=0.1, le=5.0)
    calm_s: float = 20
    attack_s: float = 40


class PhaseP99(BaseModel):
    baseline: float
    attack: float
    after: float


class RunResult(BaseModel):
    """Written to lab/results/<ts>-<scenario>-<mode>.json. Slide numbers come only from these."""
    run_id: str
    scenario: AttackName
    mode: Mode
    attacker: Optional[str] = None
    started_at: float
    victim_p99_ms: PhaseP99           # pooled over all non-attacker tenants
    victim_p99_by_tenant: Dict[str, PhaseP99] = Field(default_factory=dict)
    time_to_detect_s: Optional[float] = None    # from attack start to first incident event (if brain reachable)
    time_to_recover_s: Optional[float] = None   # from attack start until victim p99 <= 1.5x baseline for 5 s
    attacker_429: int = 0
    attacker_503: int = 0
    file: str = ""


class CellStats(BaseModel):
    """One scenario x mode in a CrashSummary."""
    scenario: AttackName
    mode: Mode
    runs: int
    failed_runs: int = 0
    victim_p99_ms_median: Optional[PhaseP99] = None
    victim_p99_ms_mean: Optional[PhaseP99] = None
    time_to_detect_s_median: Optional[float] = None
    detected_runs: int = 0
    time_to_recover_s_median: Optional[float] = None
    recovered_runs: int = 0
    attacker_429_median: float = 0.0
    attacker_503_median: float = 0.0
    brain_checked_runs: int = 0
    culprit_named_runs: int = 0
    innocents_penalised: List[str] = Field(default_factory=list)   # union over runs
    innocents_penalised_runs: int = 0
    run_files: List[str] = Field(default_factory=list)


class CrashSummary(BaseModel):
    """lab/results/summary-<ts>.json, written by `python -m tenantguard_lab scorecard`."""
    created_at: float
    lab_url: str
    brain_url: str
    scenarios: List[AttackName]
    modes: List[Mode]
    runs_per_cell: int
    attacker: Optional[str] = None
    intensity: float = 1.0
    calm_s: float = 20
    attack_s: float = 40
    aggregate_only: bool = False
    cells: List[CellStats]
    skipped: List[str] = Field(default_factory=list)
    file: str = ""


# ---------- 6. Handoff cases (Web PS3 convergence; bridge integrations/handoff on :8400) ----------
# Internal view served by the bridge (GET /cases, GET /cases/{case_id}). The partner's wire format
# ("HH:MM:SS" `time`/`deadline`, strict) exists only inside the bridge, which converts at the edge.
Verdict = Literal["costly_pattern", "over_plan", "honest_growth", "host_fault", "mitigated_no_blame"]
Severity = Literal["low", "medium", "high"]
CaseStatus = Literal["waiting_for_decision", "in_progress", "resolved", "closed"]
CaseDecision = Literal["approve", "reject", "extend", "release", "resolve"]
ActionState = Literal["pending", "approved", "rejected", "extended", "released", "resolved", "expired"]


class CaseContext(BaseModel):
    verdict: Verdict
    affected: List[str] = Field(default_factory=list)   # victim tenant ids (barcode neighbours), may be empty
    evidence_link: str                                  # http://<ui-host>:5173/case/<incident_id>
    summary: str


class CaseAction(BaseModel):
    action_id: str                # "A1", "A2", ... unique within a case
    text: str
    deadline_at: float            # epoch s; after it TenantGuard applies its safe default (TTL auto-undo)
    deadline_ist: str             # display only, "HH:MM:SS" IST
    state: ActionState = "pending"
    decided_by: Optional[str] = None
    decided_at: Optional[float] = None


class CaseHistoryItem(BaseModel):
    at: float                     # epoch s
    time_ist: str                 # display only, "HH:MM:SS" IST
    event: str


class CaseView(BaseModel):
    """Bridge GET /cases (list) and GET /cases/{case_id}."""
    case_id: str                  # "INC-0001", assigned by the bridge
    incident_id: str              # brain IncidentEvent.incident_id
    tenant: Optional[str] = None
    title: str
    severity: Severity
    status: CaseStatus
    context: CaseContext
    actions: List[CaseAction] = Field(default_factory=list)
    history: List[CaseHistoryItem] = Field(default_factory=list)
    owner: Optional[str] = None
    owner_since_at: Optional[float] = None
    owner_since_ist: Optional[str] = None
    created_at: float
    updated_at: float
    delivered_at: Optional[float] = None   # last successful POST to the Web app; None = not yet delivered


class OperatorCmd(BaseModel):
    """Human decision -> brain POST /api/operator (control token required). The brain's ladder obeys it,
    so humans and the ladder never fight.
    - release: force the tenant's ladder to normal (clear 1.0/1.0) and suppress re-escalation for until_s.
    - extend:  hold the tenant at its current level/factors for until_s (renewing TTLs), then resume the ladder.
    - resume:  cancel any operator hold now.
    until_s must be finite, 0 < until_s <= gateway.ttl_max_s, else 400.
    """
    tenant: str
    decision: Literal["release", "extend", "resume"]
    until_s: float = 120.0
    by: str
    case_id: Optional[str] = None


# ---------- 7. Billing (billing service on :8600, read-only) ----------
class BillLine(BaseModel):
    kind: Literal["compute", "background", "db", "unmetered", "sla_credit"]
    description: str                      # plain English, no other tenant named
    cpu_ms: float = 0.0
    rate_rs_per_cpu_s: float = 0.0
    amount_rs: float                      # negative for credits
    evidence: dict = Field(default_factory=dict)   # unmetered: {incident_id, confidence, servers, excess_cpu_ms}; sla_credit: {violation_s, pct}


class BillView(BaseModel):
    """Billing GET /bills (list) and GET /bills/{tenant}."""
    tenant: str
    name: str
    tier: Tier
    period_start: float
    period_end: float
    requests: int
    measured_cpu_ms: float                # handler + bg as measured
    billed_cpu_ms: float                  # after calm-reference pricing
    contention_waived_cpu_ms: float       # measured - billed (>= 0): contention we did not charge
    db_ms: float
    sla_p95_ms: float
    sla_violation_s: float
    sla_credit_pct: float
    lines: List[BillLine]
    subtotal_rs: float
    credits_rs: float                     # <= 0
    total_rs: float
    incident_refs: List[str] = Field(default_factory=list)   # own incidents: brain ids; suffered as victim: opaque "ref-<8 hex>"


WhyCause = Literal["none", "neighbor", "host_fault", "own_usage", "unknown"]


class WhyEpisode(BaseModel):
    start: float
    end: float
    cause: WhyCause
    servers_affected: int                 # of this tenant's servers
    servers_total: int
    worst_p95_ms: float
    violation_s: float
    mitigated_after_s: Optional[float] = None   # detected -> rescued (or -> first throttle of the culprit)
    sentence: str


class WhyView(BaseModel):
    """Billing GET /why/{tenant}. Never names another tenant."""
    tenant: str
    window_start: float
    window_end: float
    slow: bool
    sla_p95_ms: float
    violation_s: float
    credit_pct: float
    episodes: List[WhyEpisode]
    sentence: str


# ---------- 8. Incident narrator (narrator :8700, read-only consumer of the brain) ----------
class IncidentReport(BaseModel):
    """Operator-facing report, built deterministically from the evidence pack."""
    headline: str
    timeline: List[str]               # "HH:MM:SS stage: what happened", oldest first
    cause: str
    evidence_points: List[str]
    action_taken: str
    operator_recommendation: str


class CustomerMessage(BaseModel):
    """For an affected tenant: built from the anonymised pack, so it never names another tenant."""
    message: str


class ReportView(BaseModel):
    """Narrator GET /reports (list, newest first), GET /reports/{incident_id}, WS /ws/reports (pushed on every change)."""
    incident_id: str
    tenant: Optional[str] = None                  # primary/culprit tenant, if any
    trigger_stage: Stage                          # the stage that produced this version (named | verdict | closed)
    version: int                                  # +1 on every change for this incident
    report: IncidentReport
    customer_message: CustomerMessage
    evidence: dict                                # the evidence pack this version was built from
    created_at: float                             # epoch s
    updated_at: float


# ---------- 9. v2: pool, projects, replicas, snapshot (brain admin API and brain -> gateway) ----------
ReplicaState = Literal["starting", "ready", "draining"]


class PoolSpec(BaseModel):
    """Capacity the admin gives TenantGuard (PUT /admin/pool). No credits: real cores and MB."""
    cpu_total: float
    mem_total_mb: int
    headroom_ratio: float = 0.10


class ProjectSpec(BaseModel):
    """POST /admin/projects. Tier sets the project's weight and its tenants' request quota."""
    id: str                       # ^[a-z0-9-]{1,32}$
    name: str
    tier: Tier
    floor_cpu: float              # guaranteed cores
    cap_cpu: float                # max cores
    warm_min: int = 1             # replicas kept while idle; 0 = may sleep


class ProjectPatch(BaseModel):
    name: Optional[str] = None
    tier: Optional[Tier] = None
    floor_cpu: Optional[float] = None
    cap_cpu: Optional[float] = None
    warm_min: Optional[int] = None


class TenantCreate(BaseModel):
    """POST /admin/projects/{project}/tenants. api_key None = generate one (returned once)."""
    id: str
    name: str
    api_key: Optional[str] = None
    monthly_revenue_rs: int = 0
    protected: bool = False
    sla_p95_ms: float = 100.0


class TenantCreated(BaseModel):
    tenant: str
    project: str
    api_key: str                  # shown once; only the sha256 is stored


class ReplicaView(BaseModel):
    id: str                       # tg-<project>-<n>
    project: str
    url: str
    state: ReplicaState
    borrowed: bool = False        # above the project's own allowance; drained first when memory is needed
    nano_cpus: int = 0            # cpu.max (1e9 = one core)
    cpu_shares: int = 0           # cpu.weight
    mem_mb: int = 0
    started_at: float = 0.0
    tenants: List[str] = Field(default_factory=list)   # tenants this replica was started for (TENANTGUARD_TENANTS)


class SnapTenant(BaseModel):
    id: str
    name: str
    project: str
    tier: Tier
    key_sha256: str
    weight: float = 1.0
    quota: Quota
    monthly_revenue_rs: int = 0
    protected: bool = False
    sla_p95_ms: float = 100.0


class Snapshot(BaseModel):
    """Brain -> gateway POST /control/snapshot, every control tick and on change; GET /snapshot on the brain.
    The gateway swaps it in atomically. Receiving one also proves the brain is alive (fail-static)."""
    version: int
    issued_at: float
    tenants: List[SnapTenant]
    replicas: Dict[str, List[ReplicaView]]    # project -> replicas (gateway routes only to state == ready)
    limits: List[LimitsCmd] = Field(default_factory=list)
    pins: Dict[str, str] = Field(default_factory=dict)   # tenant -> replica id, only during a split test
    pin_ttl_s: float = 30.0


class WakeCmd(BaseModel):
    """Gateway -> brain POST /control/wake: a project with no ready replica got a request."""
    project: str


class ProjectView(BaseModel):
    id: str
    name: str
    tier: Tier
    weight: float
    floor_cpu: float
    cap_cpu: float
    warm_min: int
    tenants: List[str]
    demand_cpu: float = 0.0
    used_cpu: float = 0.0         # measured CPU, cores
    base_cpu: float = 0.0
    loan_cpu: float = 0.0
    alloc_cpu: float = 0.0
    state: Literal["asleep", "waking", "warm", "busy"] = "warm"
    replicas: List[ReplicaView] = Field(default_factory=list)


class PoolView(BaseModel):
    pool: PoolSpec
    usable_cpu: float
    usable_mem_mb: int
    floors_cpu: float
    reserved_mem_mb: int          # memory of running replicas
    projects: List[ProjectView]


StateSnapshot.model_rebuild()
