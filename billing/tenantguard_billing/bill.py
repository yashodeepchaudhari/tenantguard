"""Build BillView and WhyView (pure, no I/O). Victim-facing text never names another tenant."""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple

from tenantguard_common.contracts import BillLine, BillView, WhyEpisode, WhyView

from .meter import Meter
from .pricing import Pricer
from .settings import BillingSettings
from .timeline import Episode, LatencyLog, Timeline, violations

IST = timezone(timedelta(hours=5, minutes=30))


def hhmmss(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=IST).strftime("%H:%M:%S")


def opaque_ref(incident_id: str) -> str:
    """A victim sees this, not the brain id (inc-…-K-3 would name the culprit)."""
    return "ref-" + hashlib.sha1(incident_id.encode()).hexdigest()[:8]


def credit_pct(cfg: BillingSettings, violation_s: float) -> float:
    pct = 0.0
    for row in cfg.sla_credit_table:
        if violation_s >= row["min_violation_s"]:
            pct = float(row["pct"])
    return pct


def _r(x: float, nd: int = 4) -> float:
    return round(x, nd)


class _Ctx:
    def __init__(self, cfg: BillingSettings, meter: Meter, pricer: Pricer, tl: Timeline, log: LatencyLog,
                 now: float) -> None:
        self.cfg, self.meter, self.pricer, self.tl, self.log, self.now = cfg, meter, pricer, tl, log, now


# ------------------------------------------------------------------ bills
def calm_blackbox_rate(ctx: _Ctx, server: str) -> float:
    """Black-box CPU ms per second on a server while no incident was open (its normal vendor load)."""
    cpu = secs = 0.0
    for s in ctx.meter.blackbox:
        if s.server != server or s.t1 <= s.t0:
            continue
        if ctx.tl.overlapping(s.t0, s.t1):
            continue
        cpu += s.cpu_ms
        secs += s.t1 - s.t0
    return cpu / secs if secs else 0.0


def unmetered_lines(ctx: _Ctx, tenant: str, start: float, end: float) -> Tuple[List[BillLine], List[str]]:
    lines, refs = [], []
    rate = ctx.cfg.rate_rs_per_ms(tenant)
    for ep in ctx.tl.overlapping(start, end):
        conf = ep.culprits.get(tenant)
        if conf is None or ep.named_at is None or conf < ctx.cfg.unmetered_min_confidence:
            continue
        a, b = max(ep.named_at, start), min(ep.end if ep.end is not None else ctx.now, end)
        if b <= a:
            continue
        servers = ctx.cfg.barcode(tenant)
        excess = 0.0
        for srv in servers:
            cpu, secs = ctx.meter.blackbox_ms([srv], a, b)
            excess += max(0.0, cpu - calm_blackbox_rate(ctx, srv) * secs)
        if excess <= 0:
            continue
        lines.append(BillLine(
            kind="unmetered",
            description=(f"Unmetered background processing your workload caused on your servers during incident "
                         f"{ep.incident_id} ({conf:.0%} confidence): {excess / 1000:.2f} CPU-s above their normal level."),
            cpu_ms=_r(excess, 3), rate_rs_per_cpu_s=rate * 1000, amount_rs=_r(excess * rate),
            evidence={"incident_id": ep.incident_id, "confidence": round(conf, 3), "servers": servers,
                      "excess_cpu_ms": _r(excess, 3)}))
        refs.append(ep.incident_id)
    return lines, refs


def build_bill(ctx: _Ctx, tenant: str, start: float, end: float) -> BillView:
    cfg = ctx.cfg
    tc = cfg.tenants.get(tenant)
    rate = cfg.rate_rs_per_ms(tenant)
    req = 0
    measured_h = billed_h = bg = db = 0.0
    for b, u in ctx.meter.tenant_buckets(tenant, start - start % cfg.bucket_s, end):
        p = ctx.pricer.priced(tenant, b, u, ctx.tl)
        req += p.requests
        measured_h += p.measured_handler_ms
        billed_h += p.billed_handler_ms
        bg += p.bg_cpu_ms
        db += p.db_ms
    waived = max(0.0, measured_h - billed_h)
    lines = [BillLine(kind="compute",
                      description=(f"Request processing: {billed_h / 1000:.2f} CPU-s at calm reference prices"
                                   + (f"; {waived / 1000:.2f} CPU-s of slowdown caused by others not charged"
                                      if waived > 0.5 else "") + "."),
                      cpu_ms=_r(billed_h, 3), rate_rs_per_cpu_s=rate * 1000, amount_rs=_r(billed_h * rate)),
             BillLine(kind="background", description=f"Background jobs you triggered: {bg / 1000:.2f} CPU-s.",
                      cpu_ms=_r(bg, 3), rate_rs_per_cpu_s=rate * 1000, amount_rs=_r(bg * rate))]
    if cfg.db_price_rs_per_s > 0 or db > 0:
        lines.append(BillLine(kind="db", description=f"Database time: {db / 1000:.2f} s.",
                              amount_rs=_r(db / 1000 * cfg.db_price_rs_per_s)))
    um, own_refs = unmetered_lines(ctx, tenant, start, end)
    lines += um
    subtotal = sum(line.amount_rs for line in lines)

    sla = cfg.sla(tenant)
    viol = violations(tenant, sla, ctx.log, ctx.tl, start, end)
    viol_s = sum(t.dt for t, _ in viol)
    pct = credit_pct(cfg, viol_s)
    credits = -_r(subtotal * pct / 100) if pct else 0.0
    if pct:
        lines.append(BillLine(kind="sla_credit",
                              description=(f"Service credit: your requests were slower than your {sla:.0f} ms target "
                                           f"for {viol_s:.1f} s because of a platform incident ({pct:g}%)."),
                              amount_rs=credits, evidence={"violation_s": round(viol_s, 2), "pct": pct}))
    refs = list(own_refs)
    refs += [e.incident_id for e in ctx.tl.overlapping(start, end) if tenant in e.blamed and e.incident_id not in refs]
    refs += sorted({opaque_ref(ep.incident_id) for _, ep in viol})
    return BillView(
        tenant=tenant, name=tc.name if tc else tenant, tier=tc.tier if tc else "free",
        period_start=start, period_end=end, requests=req,
        measured_cpu_ms=_r(measured_h + bg, 3), billed_cpu_ms=_r(billed_h + bg, 3),
        contention_waived_cpu_ms=_r(waived, 3), db_ms=_r(db, 3), sla_p95_ms=sla,
        sla_violation_s=round(viol_s, 2), sla_credit_pct=pct, lines=lines,
        subtotal_rs=_r(subtotal), credits_rs=credits, total_rs=_r(subtotal + credits), incident_refs=refs)


# ------------------------------------------------------------------ why
def _servers_word(k: int, n: int) -> str:
    return f"{k} of your {n} server{'s' if n != 1 else ''}"


def _episode(ctx: _Ctx, tenant: str, ep: Episode, a: float, b: float) -> Optional[WhyEpisode]:
    cfg = ctx.cfg
    sla = cfg.sla(tenant)
    mine = set(cfg.barcode(tenant))
    lo, hi = max(ep.start, a), min(ep.end if ep.end is not None else b, b)
    ticks = ctx.log.between(lo, hi)
    late = [t for t in ticks if t.p95.get(tenant, 0.0) > sla]
    slow_mine = set().union(*(t.slow_servers for t in ticks)) & mine if ticks else set()
    blamed = tenant in ep.blamed
    if not blamed and not late and not slow_mine:
        return None

    if blamed:
        cause = "own_usage"
    elif ep.kind == "server":
        cause = "neighbor" if set(ep.culprits) - {tenant} else ("host_fault" if ep.host_suspects else "unknown")
    else:
        cause = "neighbor"
    k = len(slow_mine)
    if not k and cause == "neighbor" and ep.ladder_tenant:
        k = len(mine & set(cfg.barcode(ep.ladder_tenant)))  # servers shared with the over-user (count only)
    n = len(mine)
    s_ts, e_ts = (late[0].ts, late[-1].ts) if late else (lo, hi)
    worst = max((t.p95.get(tenant, 0.0) for t in ticks), default=0.0)
    viol_s = 0.0 if blamed else sum(t.dt for t in late)

    mitigated = None
    if ep.kind == "server" and ep.rescued_at is not None:
        mitigated = max(0.0, ep.rescued_at - ep.start)
    elif ep.kind == "tenant" and ep.throttled_at is not None:
        mitigated = max(0.0, ep.throttled_at - ep.start)

    when = f"Between {hhmmss(s_ts)} and {hhmmss(e_ts)} IST"
    if cause == "own_usage":
        text = (f"{when}, your own workload used more than its fair share of your servers, so it was slowed down "
                f"to protect other customers. No credit applies.")
    elif cause == "neighbor":
        text = f"{when}, another tenant's workload slowed {_servers_word(max(k, 1), n)}."
        if mitigated is not None and ep.kind == "server":
            text += f" Your traffic was moved to healthy servers in {mitigated:.1f} s."
        elif mitigated is not None:
            text += f" That workload was throttled after {mitigated:.1f} s."
    elif cause == "host_fault":
        text = f"{when}, a fault on {_servers_word(max(k, 1), n)} slowed your requests. No customer was at fault."
        if mitigated is not None:
            text += f" Your traffic was moved to healthy servers in {mitigated:.1f} s."
    else:
        text = f"{when}, {_servers_word(k, n)} ran slower than usual; we are still investigating the cause."
    return WhyEpisode(start=s_ts, end=e_ts, cause=cause, servers_affected=k, servers_total=n,
                      worst_p95_ms=round(worst, 2), violation_s=round(viol_s, 2), mitigated_after_s=
                      round(mitigated, 2) if mitigated is not None else None, sentence=text)


def build_why(ctx: _Ctx, tenant: str, a: float, b: float) -> WhyView:
    cfg = ctx.cfg
    sla = cfg.sla(tenant)
    eps = [e for e in (_episode(ctx, tenant, ep, a, b) for ep in ctx.tl.overlapping(a, b)) if e is not None]
    viol_s = sum(e.violation_s for e in eps)
    pct = credit_pct(cfg, viol_s)
    ticks = ctx.log.between(a, b)
    late_any = [t for t in ticks if t.p95.get(tenant, 0.0) > sla]
    if eps:
        sentence = " ".join(e.sentence for e in eps)
        if pct:
            sentence += f" A {pct:g}% service credit has been applied."
    elif late_any:
        worst = max(t.p95.get(tenant, 0.0) for t in late_any)
        sentence = (f"Between {hhmmss(late_any[0].ts)} and {hhmmss(late_any[-1].ts)} IST your requests were slower "
                    f"than your {sla:.0f} ms target (worst {worst:.0f} ms), but no platform incident was open: "
                    f"this matches your own load.")
    else:
        sentence = f"No slowdown: your requests stayed within your {sla:.0f} ms target."
    return WhyView(tenant=tenant, window_start=a, window_end=b, slow=bool(eps or late_any), sla_p95_ms=sla,
                   violation_s=round(viol_s, 2), credit_pct=pct, episodes=eps, sentence=sentence)
