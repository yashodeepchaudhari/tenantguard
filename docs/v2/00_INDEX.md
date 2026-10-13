# TenantGuard v2: full-rewrite spec (build-ready)

**What v2 is:** an admin-set pool → projects (floor, cap, tier) → tenants.
- Each project runs on its own containers, created only when needed.
- CPU floors are held by the kernel (`cpu.weight` inside one pool cgroup).
- The surplus is shaped by tier weight.
- Tenants are placed by rendezvous hashing.
- A hash split runs on demand to find hidden work.
- v1's ladder, DRR, retry budget, Gradient2 window, worker, handoff and narrator are reused (billing is not part of v2).

**Status of this folder:**
- This is an analysis only; nothing in the TenantGuard repo has changed.
- Numbers come from repo files, or are computed from them; anything else is marked *(est.)*.
- **Supersedes** `../updated/03_PROJECT_POOL_DESIGN.md`. The main change: CPU floors use kernel shares instead of 3 s software leases, which makes reclaim immediate and removes the lease machinery.

| # | File | Read it for |
|---|---|---|
| 01 | `01_COMPARISON.md` | v1 vs v2, with v1's measured whale runs; what is reused and deleted; recommendation |
| 02 | `02_ARCHITECTURE.md` | Principles, invariants, components, timing, CPU and memory enforcement, failures, security |
| 03 | `03_ALGORITHMS.md` | Every algorithm step by step, with worked numbers |
| 04 | `04_FUNCTIONS.md` | Every function: task, input → output, caller, v1 source; task index; call graph |
| 05 | `05_DATA_AND_CONFIG.md` | Contracts, yaml, example registry, tick order, APIs, storage |
| 06 | `06_DIAGRAMS.md` | 21 diagrams: context, topology, flows, state machines, 8 sequences, class, ER, chart, build graph |
| 07 | `07_BUILD_PLAN.md` | Tasks V1–V11 with files, tests, acceptance; waves; scenarios; scorecard; acceptance gate |
| 08 | `08_RISKS.md` | Risks R1–R13 with mitigations |
| 09 | `09_AS_BUILT.md` | What was built on `v2/main`: deviations and why, simulated scenarios, task status, what is left |
