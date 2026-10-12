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
# Required on every gateway /admin/* and /control/* request (brain, UI, lab send it).
# Value = env TENANTGUARD_CONTROL_TOKEN (default "dev"); compare with hmac.compare_digest. Missing/wrong -> 401.
H_CONTROL_TOKEN = "X-TenantGuard-Control-Token"


# ---------- 0. Tenancy / admin (gateway /admin/*) ----------
class Quota(BaseModel):
    rate_rps: float          # token bucket refill, requests/s
    burst: int               # token bucket capacity, requests
    cost_ms_per_s: float     # cost bucket refill, CPU-ms per second (capacity = tenantguard.yaml gateway.cost_bucket_s seconds worth)
    concurrency: int         # max in-flight requests


class TenantConfig(BaseModel):
    """Input for POST /admin/tenants and the layout file. Never returned by the API (has api_key)."""
    id: str                  # "A".."L"
    name: str
    tier: Tier
    servers: List[str]       # barcode, e.g. ["w0","w1","w2"]
    api_key: str             # demo plaintext key; gateway stores sha256 only
    weight: float            # scheduler weight (from tier)
    quota: Quota             # configured plan quota (PATCH changes this)
    monthly_revenue_rs: int = 0
    protected: bool = False


class TenantAdminView(BaseModel):
    """Output of GET /admin/tenants (list) and GET /admin/tenants/{id}."""
    id: str
    name: str
    tier: Tier
    servers: List[str]
    weight: float
    quota: Quota                     # configured quota (after PATCH)
    effective_quota: Quota           # quota after the active LimitsCmd factor
    factor: float = 1.0
    weight_factor: float = 1.0
    blocked: bool = False
    placement: str = "normal"        # normal | spare | quarantine | wN (relocation target)
    override_expires_at: Optional[float] = None
    monthly_revenue_rs: int = 0
    protected: bool = False


class QuotaPatch(BaseModel):
    rate_rps: Optional[float] = None
    burst: Optional[int] = None
    cost_ms_per_s: Optional[float] = None
    concurrency: Optional[int] = None


class ModeBody(BaseModel):
    mode: Mode


# ---------- 1. Telemetry (gateway -> brain, WS /internal/telemetry, every tick_ms) ----------
class Cell(BaseModel):
    """Requests of one tenant served by one server during the last tick (server-side latency, excludes queue wait)."""
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
    cpu_limit_cores: float = 0.5 # container CPU limit (worker fills from tenantguard.yaml worker.cpus / env), for cpu_pct
    mem_mb: float
    fault: Optional[dict] = None
    tenants: List[TenantUsage] = Field(default_factory=list)
    classes: List[ClassCost] = Field(default_factory=list)


# ---------- 3. Control (brain -> gateway, POST /control/{kind}) ----------
class ControlBase(BaseModel):
    incident_id: Optional[str] = None
    ttl_s: float = 60.0           # gateway reverts the override after this many seconds


class RouteWeightsCmd(ControlBase):
    weights: Dict[str, Dict[str, float]]      # tenant -> server -> weight (normalized by gateway)


class RelocateCmd(ControlBase):
    tenant: str
    cls: Optional[str] = None                 # None = whole tenant
    target: Optional[str] = None              # "spare" | "quarantine" | "wN"; None = clear relocation


class LimitsCmd(ControlBase):
    """Throttle / deprioritize one tenant.

    - The latest command for a tenant REPLACES the previous one (both factors) and restarts the TTL.
    - Base = the tenant's current configured quota (after any PATCH), not the tier default.
    - Effective int fields = max(1, round(base * factor)); floats = base * factor.
    - factor == 1.0 and weight_factor == 1.0 clears the override immediately.
    - Gateway returns 400 if factor <= 0 or factor > 1, or weight_factor <= 0 or > 1
      (checked by hand in the handler; do NOT add Field constraints here, they would give 422).
    - SENDERS MUST ALWAYS SEND BOTH factors from their current state; an omitted field
      defaults to 1.0 and silently un-throttles that dimension.
    """
    tenant: str
    factor: float = 1.0                       # multiplies rate, burst, cost and concurrency
    weight_factor: float = 1.0                # multiplies scheduler weight


class DrainCmd(ControlBase):
    server: str
    drained: bool = True


class BlockCmd(ControlBase):
    tenant: str
    blocked: bool = True                      # blocked=false clears immediately


class ReferenceCostsCmd(BaseModel):
    costs: Dict[str, float]                   # cls -> reference CPU ms


class ChargeCmd(BaseModel):
    """Brain -> gateway POST /control/charge: CPU a tenant used OUTSIDE the request path (background jobs), debited from its cost bucket.

    Not an override: no TTL, applied once, only in `tenantguard` mode (otherwise acked with detail "ignored: mode").
    Gateway returns 400 if cpu_ms < 0 or not finite, or the tenant is unknown.
    `charge_id` makes resends idempotent (the gateway remembers the last 1024 ids); None = always apply.
    """
    tenant: str
    cpu_ms: float                 # CPU ms to debit
    period_s: float = 1.0         # interval the charge covers (informational, for metrics)
    charge_id: Optional[str] = None


class Heartbeat(BaseModel):
    ts: float                                 # sender's clock; gateway uses its OWN receive time for fail-static


class ControlAck(BaseModel):
    ok: bool
    detail: str = ""


class OverridesView(BaseModel):
    """Output of gateway GET /admin/overrides."""
    limits: Dict[str, LimitsCmd] = Field(default_factory=dict)        # tenant -> active cmd
    blocks: Dict[str, BlockCmd] = Field(default_factory=dict)
    relocations: List[RelocateCmd] = Field(default_factory=list)
    route_weights: Dict[str, Dict[str, float]] = Field(default_factory=dict)
    drains: Dict[str, DrainCmd] = Field(default_factory=dict)
    expires_at: Dict[str, float] = Field(default_factory=dict)       # "<kind>:<key>" -> epoch s
    last_heartbeat_age_s: Optional[float] = None
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
    placement: str = "normal"     # normal | spare | quarantine | wN (same values as TenantAdminView)


class ServerView(BaseModel):
    server: str
    p95_ms: float                 # server-side p95 over the last 2 s
    peer_ratio: float             # p95 / median p95 of other servers (Wave 2; 1.0 until then)
    cpu_pct: float                # 0..100 of the container CPU limit (cpu_limit_cores)
    status: Literal["healthy", "slow", "drained"]


class StateSnapshot(BaseModel):
    type: Literal["state"] = "state"
    ts: float
    mode: Mode
    tenants: List[TenantView]
    servers: List[ServerView]
    utilization: float            # 0..1 of fleet CPU limit (w0..w8)
    fairness: float               # Jain index 0..1 over per-tenant (completed rps / weight) of active tenants
    status_sentence: str
    brain_ok: bool = True
    noisy_thresholds: Dict[str, float] = Field(default_factory=dict)   # e.g. {"warn":1.5,"throttle":2.0,"quarantine":4.0} from brain.noisy; UI must use these, not its own copies


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
