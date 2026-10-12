"""Shared message contracts between gateway, workers, brain, lab and UI.

Owned by the coordinator. Every cross-service JSON message is one of these models.
Change only through the coordinator (see docs/PLAN.md §3).
"""
from __future__ import annotations

from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, Field

Mode = Literal["off", "industry", "padosi"]
Tier = Literal["enterprise", "pro", "free"]
Level = Literal["normal", "warn", "throttle", "deprioritize", "quarantine", "blocked"]

# Headers set by the gateway on every forwarded request (workers trust only these).
H_TENANT = "X-Padosi-Tenant"
H_CLASS = "X-Padosi-Class"          # request class hash computed by gateway
H_REQUEST_ID = "X-Request-Id"
# Header set by the worker on every response: handler CPU ms spent on this request.
H_CPU_MS = "X-Padosi-Cpu-Ms"


# ---------- 0. Tenancy / admin (gateway /admin/*) ----------
class Quota(BaseModel):
    rate_rps: float          # token bucket refill
    burst: int               # token bucket capacity
    cost_ms_per_s: float     # CPU-ms budget refill per second (cost bucket)
    concurrency: int         # max in-flight requests


class TenantConfig(BaseModel):
    id: str                  # "A".."L"
    name: str
    tier: Tier
    servers: List[str]       # barcode, e.g. ["w0","w1","w2"]
    api_key: str             # demo plaintext key; gateway stores sha256 only
    weight: float            # scheduler weight (from tier)
    quota: Quota
    monthly_revenue_rs: int = 0
    protected: bool = False


class QuotaPatch(BaseModel):
    rate_rps: Optional[float] = None
    burst: Optional[int] = None
    cost_ms_per_s: Optional[float] = None
    concurrency: Optional[int] = None


class ModeBody(BaseModel):
    mode: Mode


# ---------- 1. Telemetry (gateway -> brain, WS /internal/telemetry, every 250 ms) ----------
class Cell(BaseModel):
    """Requests of one tenant served by one server during the last tick."""
    tenant: str
    server: str
    count: int
    p50_ms: float
    p95_ms: float
    errors: int = 0


class QueueStat(BaseModel):
    tenant: str
    depth: int
    wait_p95_ms: float
    admitted: int            # during the tick
    rejected: int            # 429/503 during the tick
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


# ---------- 2. Worker stats (worker GET /stats, polled at 1 Hz; counters CUMULATIVE since start) ----------
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
    mem_mb: float = 0.0


class ClassCost(BaseModel):
    tenant: str
    cls: str
    count: int
    cpu_ms_total: float


class WorkerStats(BaseModel):
    server: str
    ts: float
    cpu_total_ms: float          # whole process tree
    blackbox_cpu_ms: float       # unattributed "vendor indexer" CPU
    mem_mb: float
    fault: Optional[dict] = None
    tenants: List[TenantUsage] = Field(default_factory=list)
    classes: List[ClassCost] = Field(default_factory=list)


# ---------- 3. Control (brain -> gateway, POST /control/{kind}) ----------
class ControlBase(BaseModel):
    incident_id: Optional[str] = None
    ttl_s: float = 60.0           # gateway reverts the override after this


class RouteWeightsCmd(ControlBase):
    weights: Dict[str, Dict[str, float]]      # tenant -> server -> weight


class RelocateCmd(ControlBase):
    tenant: str
    cls: Optional[str] = None                 # None = whole tenant
    target: str                               # "spare" | "quarantine" | "wN"


class LimitsCmd(ControlBase):
    tenant: str
    factor: float = 1.0                       # multiplies rate, burst, cost and concurrency of the plan quota (0<f<=1 throttles)
    weight_factor: float = 1.0                # multiplies scheduler weight (deprioritize)


class DrainCmd(ControlBase):
    server: str
    drained: bool = True


class BlockCmd(ControlBase):
    tenant: str
    blocked: bool = True


class ReferenceCostsCmd(BaseModel):
    costs: Dict[str, float]                   # cls -> reference CPU ms


class Heartbeat(BaseModel):
    ts: float


class ControlAck(BaseModel):
    ok: bool
    detail: str = ""


# ---------- 4. Brain -> UI (WS /ws/events on the brain) ----------
class TenantView(BaseModel):
    tenant: str
    name: str
    tier: Tier
    p95_ms: float
    rps: float
    rejected_rps: float
    cpu_share: float              # 0..1 of fleet CPU
    noisy_score: float            # 0..1
    level: Level
    placement: str = "normal"     # normal | spare | quarantine


class ServerView(BaseModel):
    server: str
    p95_ms: float
    peer_ratio: float
    cpu_pct: float
    status: Literal["healthy", "slow", "drained"]


class StateSnapshot(BaseModel):
    type: Literal["state"] = "state"
    ts: float
    mode: Mode
    tenants: List[TenantView]
    servers: List[ServerView]
    utilization: float            # 0..1
    fairness: float               # Jain index 0..1
    status_sentence: str


Stage = Literal["detected", "level_change", "rescued", "named", "proving",
                "verdict", "resolved", "explained", "closed"]


class Suspect(BaseModel):
    tenant: str
    confidence: float


class IncidentEvent(BaseModel):
    type: Literal["incident"] = "incident"
    incident_id: str
    stage: Stage
    ts: float
    suspects: List[Suspect] = Field(default_factory=list)
    verdict: Optional[str] = None
    action: Optional[str] = None
    detail: str = ""
    report: Optional[dict] = None


# ---------- 5. Lab (lab service, POST /lab/attack) ----------
AttackName = Literal["flood", "whale", "hog", "memory", "retry_storm", "background_bomb",
                     "blackbox_bomb", "cache_flood", "faulty_server", "honest_spike"]


class AttackReq(BaseModel):
    attack: AttackName
    tenant: Optional[str] = None      # faulty_server uses server instead
    server: Optional[str] = None
    intensity: float = 1.0
    duration_s: float = 60.0


class AttackStatus(BaseModel):
    attack_id: str
    attack: AttackName
    tenant: Optional[str] = None
    server: Optional[str] = None
    started_at: float
    ends_at: float
