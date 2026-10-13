# 07: Unique features

What TenantGuard does that a rate limiter, or a typical fair-share API gateway, does not. Each item is built and merged on `master`, and each says where it lives.

```mermaid
flowchart LR
  RL["Typical rate limiter<br/>counts requests"] -->|"+ CPU-cost quotas<br/>+ retry budget<br/>+ fair lanes"| IND["Industry mode<br/>(a strong gateway)"]
  IND -->|"+ server health<br/>+ rescue<br/>+ barcode naming<br/>+ proof by relocation<br/>+ five verdicts<br/>+ background-CPU charge-back<br/>+ adaptive ladder with TTL<br/>+ human holds<br/>+ fair billing<br/>+ explanations"| TG["TenantGuard mode"]
```

## 1. Quotas on real resources, measured by the worker
A rate limiter counts requests; TenantGuard's cost bucket is debited with the **CPU each request actually used**, measured on the worker with the handler thread's CPU clock and returned in `X-TenantGuard-Cpu-Ms`. A tenant sending few but heavy requests is refused with reason `cost` and an accurate `Retry-After`. Concurrency and a retry budget are separate dimensions, and every refusal names the dimension that failed.
Where: `worker/app/meter.py`, `gateway/tenantguard_gateway/buckets.py`, `admission.py`, `core.py`.

## 2. Seeing harm nobody bills for
`worker/calibration.json` shows the problem: `POST /api/ingest` reports **0.066 ms** of handler CPU but causes **20.25 ms** in an unattributed black-box indexer. No request meter can see that. TenantGuard does not need to: it watches each server's latency against its peers, so the harm shows up wherever it lands.
Where: `worker/app/blackbox.py`, `brain/tenantguard_brain/health.py`.

## 3. Naming the culprit with a "barcode"
Tenants are placed on servers with an AG(2,3) design: each tenant gets 3 of 9 servers, and **any two tenants share at most one server**. The set of slow servers therefore decodes to the tenant whose barcode matches, with a confidence. If one server is slow on its own, no barcode explains it, so it is a host fault and nobody is blamed. The tests show every single culprit and **all 66 culprit pairs** are named exactly.
Where: `layouts/ag23.json`, `brain/tenantguard_brain/decoder.py`, `stage3.py`.

## 4. Victims first: rescue before punishment
When servers turn slow, the brain first moves the innocent tenants to their other, healthy servers (route weights), keeping every server under the 70% capacity guard. The throttle ladder only acts if harm persists. Victims recover in seconds without anyone being throttled.
Where: `brain/tenantguard_brain/rescue.py`, `gateway/tenantguard_gateway/router.py`.

## 5. A graduated, self-recovering throttle ladder
The ladder goes normal → warn → throttle (AIMD × 0.5 steps) → deprioritize (scheduler weight × 0.25) → quarantine, with hold times, hysteresis and step-wise recovery (+0.1 per tick). A tenant is flagged only if it over-uses **and** its neighbours are hurt, so a legitimate burst with no victims never leaves `normal` (tested).
Where: `brain/tenantguard_brain/score.py`, `ladder.py`, `core.py`.

## 6. Every action undoes itself
Every control command carries a TTL (finite, ≤ 1 hour) and reverts on its own; the brain renews it only while needed. If the brain stops sending heartbeats, the gateway keeps its last configuration (fail-static), but the TTLs still expire. A dead control plane can never leave a tenant punished.
Where: `gateway/tenantguard_gateway/overrides.py`, `app.py`.

## 7. Humans and automation never fight
An operator's release / extend / resume goes to the brain as an `OperatorCmd`, and the ladder obeys the hold. A block always needs a human approval and is TTL-bound.
Where: `brain/tenantguard_brain/core.py` (`operator`), `integrations/handoff/`.

## 8. Fair scheduling that keeps the queue where it can be ordered
Three weighted-DRR lanes (express › interactive › batch):
- **Express** requests use reserved headroom.
- **Batch** is shed first when a worker saturates.
- **Window:** a per-worker gradient window, computed on *excess* latency (latency minus the request's own CPU), shrinks when a worker is congested, so queueing happens at the gateway, in fair order, instead of inside the worker.

In a live comparison with a whale saturating a worker, the express lane cut the victim's gateway queue wait from a median of 358 ms to 49 ms (development laptop, not a slide number).
Where: `gateway/tenantguard_gateway/scheduler.py`, `limiter.py`, `core.py`.

## 9. An honest baseline: three switchable modes
`off`, `industry` and `tenantguard` run in the same gateway, switched live. `industry` is everything a good gateway does on its own (CPU-cost quotas, retry budget, fair lanes, adaptive window), so the comparison with `tenantguard` isolates exactly what the adaptive loop adds. The lab writes one measured result file per run, and the scorecard runner (`lab/tenantguard_lab/scorecard.py`) aggregates them. On the Docker host (whale and flood, 3 runs per cell) both protected modes held the victims close to baseline while OFF did not (whale median victim p99 during the attack: OFF 691 ms, Industry 176 ms, TenantGuard 183 ms). On these two attacks Industry ≈ TenantGuard, because the gateway limits they share contain the attacker first; the brain's own value is shown in §13 and §14.
Where: `gateway/tenantguard_gateway/core.py`, `lab/tenantguard_lab/scenario.py`, `scorecard.py`, `lab/results/`.

## 10. Every step is explained
- **Brain events:** every event carries a plain-English `detail` (why), and every action carries its command and TTL.
- **Dashboard:** the UI shows tiles, the server grid, the incident timeline, a flow view and an impact panel (revenue protected, customers shielded, innocents penalised).
- **Narrator:** writes a deterministic incident report per incident and an anonymised customer message that never names another tenant or a server.

Where: `brain/`, `ui/src/pages/`, `narrator/`.

## 11. Built for the on-call handover (Web PS3 convergence)
Every incident becomes a case in the partner's Digital Handoff app, with:
- an owner;
- pending actions with deadlines;
- history, sent again on every change (latest-wins, never blocking).

The engineer's acknowledge, approve, extend, release or resolve flows back and takes effect live. A missed deadline falls back to TenantGuard's safe default (the timers).
Where: `integrations/handoff/`.

## 12. Security as a feature
- **Control token:** required on every control and admin endpoint.
- **Path guard:** forwards the raw path, so traversal to worker admin endpoints is impossible.
- **Bounded inputs:** TTLs and quotas must be finite and within limits.
- **Body handling:** bodies are capped, and are not read before authentication.
- **Partner endpoint:** fail-closed.
- **Isolation:** tenant data is isolated per SQLite file, and the repository takes the tenant only from context.

Where: `gateway/tenantguard_gateway/security.py`, `app.py`; `worker/app/tenancy.py`, `store.py`.

## 13. Proof before punishment: relocation to the spare
Naming is a hypothesis. Before anyone is penalised, the brain tests it: the named culprit is moved to the spare worker, which is resized through the Docker socket to `spare_sizing` (1.25) × the culprit's CPU footprint. After `prove_settle_s` (2 s) the brain measures for `prove_window_s` (6 s) and computes R = 1 − post/pre slowdown on the culprit's old servers. The proof is confirmed only if R ≥ `recovery_R` (0.5) **and** the harm followed the tenant to the spare. A culprit that pauses during the test is not confirmed. A host suspect gets the matching test: it is drained and measured idle against its peers.

In the host acceptance run (2026-10-13), tenant K as a big-plan whale was named with 99% confidence and proved with **R = 0.88**. Servers `w1` and `w8` were *held hosts*: they were judged on K first and never drained as faulty hosts.
Where: `brain/tenantguard_brain/prover.py` (`DockerProver`, `HostProver`, `EngineDocker`), `stage3.py`.

## 14. Five verdicts, each with its own action
A proof is not a punishment. The verdict decides what happens, and every verdict event lists its reasons:

| Verdict | When | Action (TTL-bound) |
|---|---|---|
| `mitigated_no_blame` | confidence below 0.8, or the proof did not confirm | none; the rescue keeps the neighbours safe |
| `over_plan` | confirmed, and admission already refuses ≥ 20% of its requests | back home, limits tightened through the ladder |
| `costly_pattern` | confirmed, and its paid CPU explains < 35% of its footprint (or black-box / eviction evidence) | its most expensive request class goes to the quarantine heavy lane; the rest goes home |
| `honest_growth` | confirmed, and its paid work explains the load | stays on the spare as a burst lane, no penalty |
| `host_fault` | a drained server is still ≥ 1.5× its peers while idle | stays drained; no tenant blamed |

An honest, growing customer therefore gets *more* room, not a throttle. In the acceptance run K's verdict was `honest_growth`, and a fault on `w4` was judged `host_fault` at **5.1×** its peers while idle, with no tenant blamed. In calm traffic the brain made 0 ladder moves (2 real slowdowns of `w7` were rerouted, no tenant blamed), and 0 overrides were left at the end.
Where: `brain/tenantguard_brain/verdicts.py`, `stage3.py`.

## 15. Hidden work is charged to the tenant that caused it
Background jobs run after the request has returned, so their CPU is not in `X-TenantGuard-Cpu-Ms`. Once a second, in `tenantguard` mode, the brain sends each tenant's background-job CPU to the gateway as a `ChargeCmd`, which is debited from that tenant's own cost bucket. Each charge has an idempotent `charge_id`, and a failed send is folded into the next one.
Where: `brain/tenantguard_brain/core.py` (`_charges`), `gateway/tenantguard_gateway/core.py` (`charge`), `app.py`.

## 16. Fair billing and "Why was I slow?"
The billing service turns worker counters and brain incidents into per-tenant bills:
- **Victims never pay for contention.** During an incident a victim pays, per request class, at most the calm reference price; the waived CPU is shown on the bill.
- **The culprit pays what it used**, including an "unmetered" line for black-box CPU when it was named with enough confidence (`unmetered_min_confidence` 0.6).
- **SLA credits** follow the tenant's own `sla_p95_ms` and a credit table (2% after 2 s of violation, up to 25% after 300 s).
- **"Why was I slow?"** answers in plain English for a time window: a neighbour, a host fault, the tenant's own usage, nothing, or unknown. It never names another tenant.

Where: `billing/tenantguard_billing/` (`meter.py`, `pricing.py`, `timeline.py`, `bill.py`, `main.py`).

## Not included
- **Second-wave lab attacks** (black-box bomb, background bomb, cache flood, faulty server, honest spike) are not included: the lab answers 501 for them. The demo uses flood, whale, hog, memory and retry storm, and triggers the faulty-server scene through the worker's `/admin/fault`.
