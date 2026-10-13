# Judge Demo Guide: TenantGuard on the dashboard (about 8 minutes)

Each step gives **what to click**, **what the judges see**, **what to say** and **which PS item it proves**.

---

## 0. Before the judges arrive (10 minutes earlier)

1. **Friend's laptop (Docker host):** plugged in, Docker Desktop open, Tailscale connected. Leave it alone.
2. **Your laptop:** close VS Code and every app you don't need (memory).
3. Check the stack, from your laptop:
   ```
   curl http://100.112.232.8:8200/healthz      # "ok": true, "mode": "tenantguard"
   curl http://100.112.232.8:8300/lab/status   # "calm_running": true
   ```
4. Open **http://127.0.0.1:5173** in Chrome or Edge, press **F11** (full screen), then **Ctrl+F5**.
5. Check:
   - the top badge says **"Calm traffic · no attack"**
   - the mode switch shows **TenantGuard**
   - 12 tiles say *"within its fair share"*
6. Open a second tab with the **Scorecard** page. You'll use it at the end.
7. Decide **who clicks** (one person) and **who talks** (one person).

---

## 1. Problem and setup (45 s)

**Show:** the Control Room as it is.

**Say:**
> "Many customers, called tenants, share the same servers. One heavy tenant can slow everyone else down: the noisy neighbour. A simple API rate limit doesn't solve it, because one request can cost 1 ms or 1 second of CPU. We built TenantGuard: real isolation, measured per tenant, with adaptive policies."

**Point at:**
| On screen | Say | PS item |
|---|---|---|
| The 12 tiles (A Aarogya Health … L LokVani News) | "12 tenants, each identified by its own API key, with a tier: Enterprise, Pro or Free" | **Tenant identity** |
| The numbers on each tile (p95 ms, rps, quota %) | "Live latency and load per tenant" | **Usage metrics** |
| The 3×3 server grid | "9 real Docker containers, each limited to half a CPU and 256 MB, on the second laptop" | **Local containers** |
| The fairness and fleet CPU gauges | "Fairness near 1 means everyone gets their share" | Efficient utilization |

---

## 2. Tenant identity and quotas (45 s): the Tenants page

**Click:** top menu → **Tenants**.

**Show:** the table of 12 tenants, their tier, and their quotas (requests/s, CPU-ms per second, concurrency).

**Say:**
> "Quotas are on **resources**, not just requests: a CPU-time budget measured by the server itself, concurrency, memory per tenant, and each tenant has its own database file."

**Do (optional):** edit **K**'s rate to a smaller value, then **Save**. Show it applied. **Change it back** before you continue.

**PS:** **Resource quotas** · **Admin API** · *"not just a rate limit"*

---

## 3. The problem with protection OFF (45 s)

**Click:** back to **Control Room** → mode switch → **OFF**.
**Attack launcher:** attack **flood**, tenant **K**, duration **30** → **Launch**.

**Judges see:**
- the badge **"Attack: flood on K"**
- the fairness gauge drops (about 0.3)
- K hogs the shared servers, and other tiles' latency rises

**Say:**
> "With no protection, one tenant's flood takes an unfair share of the servers everyone uses. This is the noisy-neighbour problem."

**PS:** *"Demonstrate the system under deliberately abusive workload patterns"*

---

## 4. A rate limiter handles the easy case (45 s)

**Click:** mode → **Industry** (wait for the flood to finish, or launch it again).

**Judges see:** K's tile shows **refused requests** (429s), and the other tenants recover.

**Say:**
> "Industry mode is what most platforms do: rate limits, CPU-cost limits and a fair queue. It stops an obvious flood."

**PS:** **Rate limiting** · **Workload scheduler** (fair queue)

---

## 5. ⭐ Why a rate limit is NOT isolation (1 min)

**Still in Industry.** **Attack launcher:** **blackbox_bomb**, tenant **K**, duration **45** → **Launch**.

**Judges see:**
- **almost no refused requests** for K
- yet K's servers (w1, w3, w8 in the grid) **slow down**, and other tenants on them get slower

**Say:**
> "Now K sends cheap-looking requests that trigger heavy **hidden** work in a background component. We measured it: one request shows **0.07 ms** of CPU to K's meter but costs the server **20 ms**. The rate limiter sees nothing. This is exactly what the PS warns about: a rate limit is not isolation."

**PS:** *"Do not equate a simple API rate limit with full tenant isolation"*

---

## 6. ⭐ TenantGuard catches it (2 min): the key scene

**Click:** mode → **TenantGuard**. Launch the **same blackbox_bomb on K**, duration **75**. Then click **Flow** (top right).

**Judges see, in order (about 5–15 s apart):**
1. K's servers turn red in the grid.
2. **Flow view:** curved **↷ lines**: the victims (A, B, C, D, E, F, G, H, I) are **rerouted** to their healthy servers. The hint says *"↷ N tenants rerouted around w1, w3, w8"*.
3. The headline: **"K watched / throttled · 11 protected"**.
4. The **incident timeline** shows **detected → rescued → named (K, about 97%) → proving → verdict**.

**Click:** the incident in the **timeline** → the **Case File** opens.

**Show in the Case File:**
- **Suspects** with confidence bars (K about 97%)
- **The barcode:** K is the only tenant whose 3 servers are all slow
- **The proof animation:** K moved to the **spare** server, and the slowdown followed it
- **The verdict:** *"costly_pattern: K's paid CPU explains only 8% of the harm"*, plus the action and its auto-undo timer

**Say:**
> "First we **protect the victims**: in about a second their traffic moves away from the slow servers. Then we **name the culprit**: every tenant uses a unique combination of 3 servers, like a barcode, and only K's barcode covers all the slow servers. Then we **prove it**: we move K to a spare server, and the slowdown moves with it. Only then do we act, with a timer and automatic undo. No guessing, and nobody innocent is punished."

**PS:** **Noisy-neighbor detector** · **Adaptive throttling** · **Usage metrics**

---

## 7. A faulty server, no tenant blamed (1 min)

**Click:** **Tiles** view. **Attack launcher:** **faulty_server**, server **w4**, duration **40** → **Launch on w4**. Then switch to **Flow**.

**Judges see:**
- w4 turns red
- **↷ lines**: B, E, G and L rerouted around w4
- Timeline: **"Host suspect w4 … No tenant blamed"** → **verdict host_fault** (still slow even when idle)

**Say:**
> "Not every slowdown is a tenant's fault. Here the server itself is sick. TenantGuard tests it idle, calls it a host fault, moves the tenants away and blames nobody."

**PS:** **Noisy-neighbor detector** (it tells a noisy tenant from a faulty host)

---

## 8. Efficiency: an honest burst is NOT punished (45 s)

**Attack launcher:** **honest_spike**, tenant **J** (JhatpatMart, Enterprise), duration **30** → **Launch**.

**Judges see:** J's traffic rises, J's tile stays **NORMAL** (*"within its fair share"*), and fleet CPU rises.

**Say:**
> "The PS says isolation must coexist with efficiency. J has its sale day: real growth within its plan. It uses idle capacity and is **not** throttled."

**PS:** *"Strong isolation must coexist with efficient resource utilization"*

---

## 9. Measured proof (1 min): the Scorecard and ₹ impact

**Click:** top menu → **Scorecard**.

**Show:** OFF vs Industry vs TenantGuard, measured on 18 runs:
- **Whale with protection OFF:** victims **4.6× slower** (p99 149 → 691 ms)
- **With protection:** victims stay normal (about 160–180 ms)
- **0 innocent tenants penalised** in every run

**Click:** back to **Control Room** and point at the **₹ impact panel**: revenue protected, SLO breach avoided, **innocents penalised: 0**.

**Say:**
> "Every number here was measured on the real system, not simulated."

**PS:** **Usage metrics** · *"measuring tenant-level latency and resource consumption"*

---

## Optional: Jury challenge (1–2 min), only after a rehearsal on the Docker host

**Open:** **http://127.0.0.1:5173/challenge** in a new tab (it has no menu tab, so the header stays as it is). Mode **TenantGuard**, calm traffic on. Wait until the cards are clickable: the page waits for no attack, no scenario run, nobody held back and no open incident.

**Hand the judge the laptop:** they pick what happens, secretly:
- **A company attacks:** Heavy searches, Flood or Hidden work, then they tap a company.
- **Or nobody is to blame:** Honest growth (tap a company) or Broken server (tap w0–w8).

**Judges see:** the choice sealed as a full SHA-256 hash (ask a judge to photograph it), a timer and the 9 servers. TenantGuard either names a company (with its noisy score, CPU share, neighbours over the 150 ms target and the brain's own words) or blames nobody within 45 s. **Open the seal** stops the round, shows the judge's real choice and the exact sealed text (the judges can hash it on their own phone), gives ✓ or ✕, and lists any company held back.

**Say:**
> "We don't choose the attacker, you do. TenantGuard only sees server measurements. And when nobody is to blame, the right answer is to blame nobody."

**Caution:** the result is whatever the live brain does, and a ✕ is shown as a ✕. Rehearse all five choices on the Docker host first. In mock mode (`npm run dev:mock`, open `/challenge?script=0`) the simulation plays the brain, so its ✓ proves nothing (and it wrongly throttles an honest spike).

**PS:** **Noisy-neighbor detector** · *"Do not equate a simple API rate limit with full tenant isolation"*

---

## 10. Close (20 s)

**Say:**
> "Tenant identity, resource quotas, rate limiting, a fair scheduler, per-tenant metrics, a noisy-neighbour detector with proof, adaptive throttling that undoes itself, and an admin dashboard. Every tenant gets its fair share, and every slowdown gets a name."

---

## If a judge asks…

| Question | Answer (and what to show) |
|---|---|
| "Isn't this just rate limiting?" | Step 5: the vendor bomb gets almost no refusals, yet victims suffer. Step 6: TenantGuard still catches it |
| "How do you know it's K and not a server?" | Case File: barcode + relocation proof. Step 7: a faulty server is called host_fault, with no tenant blamed |
| "Won't you punish a good customer?" | Step 8: honest spike not throttled; the scorecard shows 0 innocents penalised; every action has a timer and auto-undo |
| "Is it real or simulated?" | Real Docker containers with CPU/memory limits on a second laptop; only the customers' traffic is simulated |
| "What about memory / database?" | Per-tenant memory budget + container memory limits; one database file per tenant; DB time metered per tenant. (Network: request-size limits and byte metering only) |
| "Where is the admin API?" | `http://100.112.232.8:8000/docs`: every /admin endpoint, password-protected |

## If something goes wrong
- **Dashboard blank:** Ctrl+F5.
- **Stuck in the wrong mode:** click **TenantGuard**.
- **An attack keeps running:** click **Stop** in the launcher.
- **Nothing works:** run `python tools/demo.py --step` (the same scenes, driven automatically), or show `prototype-docs/` and the screenshots in `ui/screenshots/`.
