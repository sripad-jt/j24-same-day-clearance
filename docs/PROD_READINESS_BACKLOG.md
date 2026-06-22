# J24 Same-Day Clearance — Production Readiness Backlog

## Context

`j24-same-day-clearance` is a Temporal markdown agent + React control plane that clears
short-shelf-life perishables by walking a deterministic price ladder (list → 25% → 50% → ₹1)
across the day, with owner approval. It runs **end-to-end today on synthetic stubs**; one
real integration (Inventory Item Details read) is wired live for the BTM Layout pilot. This
backlog enumerates everything still pending to take it to **prod**, grouped by priority.

**Pilot facility:** J24 – Essentials BTM Layout (`BZID-1304298141` / `FACIL-1441684082`),
scope = leafy greens, master shelf-life L=1.

**Current live vs stub state**
- ✅ LIVE: Inventory Item Details read via Bolt Gateway (`adapters/_bolt.py`,
  `INVENTORY_SOURCE=live`); real sell-through via OUTWARDED count (`adapters/inventory.py::live_units_sold`).
- 🟡 STUB (synthetic): catalog, Q0/opening stock, price anchor, goldeneye (price write),
  notify (owner cards/push), retailmedia (second screen), copy_llm.

---

## ✅ Completed / already built

**Core engine & control plane (working end-to-end on the demo stack)**
- **Temporal workflow** — `PerishableMarkdownWorkflow` (`workflows/markdown.py`): full
  ladder loop, timers, signals (`owner_decision`, `additional_grn`, `sold_out`,
  `manual_override`), `current_state` query, determinism boundary respected.
- **Deterministic pricing engine** — pure `pricing/decision_engine.py::decide()`
  (hold vs step vs auto-clear, monotonic non-increasing), **unit-tested** in `tests/`.
- **Price ladder config** — `pricing/ladder.py` (list → 25% → 50% → ₹1, snapshotted at
  run start via `plan_run`).
- **Activities layer** — all I/O wrapped in `@activity.defn` (`activities/pipeline.py`)
  with a per-activity retry/timeout matrix (`_READ/_NOTIFY/_APPLY/_LLM/_DB`).
- **DB read-model + idempotent price ledger** — `db/repo.py`; `record_price_change`
  keyed on `(run_id, rung)` so retries/replays never double-apply.
- **FastAPI control plane** — `api/main.py`: list/inspect runs, seed runs, audit trail,
  inventory endpoint, and all owner signals (decision/override/grn/soldout).
- **React/Vite web app** — Dashboard, Run detail (ladder + reason trail), Approvals queue,
  Config (`web/`).
- **Owner approval round-trip (internal/simulated)** — approval card → `wait_condition`
  → `owner_decision` signal → `apply_price` → `publish_offer`, driven by the web app.
- **Shadow mode** (log recommendations, no price writes) and **demo-speed** clock
  compression (13h day → ~30s).
- **Docker-compose stack** (Postgres + Temporal + worker + api + web) and **Temporal
  Cloud** support via API-key auth (no code change).

**Live integrations (real Jumbotail services)**
- ✅ **Inventory Item Details API wired** to Bolt Gateway (`adapters/_bolt.py`,
  `INVENTORY_SOURCE=live`, creds in `.env`) for the BTM Layout pilot facility.
- ✅ **Real sell-through for slow movers** via OUTWARDED count
  (`adapters/inventory.py::live_units_sold`), with synthetic fallback + `low_confidence`
  flag on timeout. *(Caveat: the rate math on top of this is suspect — see P0 #1; the read
  is done, the derived rate is not yet trustworthy.)*
- ✅ **Store directory** (`shared/stores.py`, 64-store J24 map) and **curated leafy-green
  catalog** (`adapters/catalog.py`, 9 JPINs) for the pilot.

> Net: the **whole loop runs end-to-end** and **one read integration is live**. Everything
> still pending below is either a data-trust issue (P0) or a real *write*/notify integration
> (P1) — the orchestration around them is already built.

---

## P0 — Data-integrity blockers (agent decisions are untrustworthy until fixed)

These feed the deterministic `decide()` directly. Until resolved, the ladder runs on
synthetic inputs and cannot be trusted to set real prices in prod.

1. **Sell-through RATE math is unverified and looks wrong** *(correctness)*
   - The decision engine (`pricing/decision_engine.py::decide`) consumes two quantities:
     `units_sold` = **cumulative units sold since today's T0**, and `run_rate` = **units/hour
     going forward**. It projects `proj = units_sold + run_rate * remaining_h`, then
     `ratio = proj / q0` against **today's** opening stock.
   - The live path (`activities/pipeline.py::fetch_sellthrough`) does NOT produce those
     semantics:
     - `sold = live_units_sold(... _since_ms(24h))` = OUTWARDED count over a **fixed 24h
       trailing window** (`INVENTORY_SELLTHROUGH_WINDOW_H=24`), i.e. since `now − 24h`,
       **not since today's T0**. So it includes **yesterday's** sales tail → not comparable
       to today's `q0` (can even exceed it → spurious HOLD).
     - `rate = sold / max(nominal_elapsed_h, 0.5)` divides a **24h-window** count by
       **elapsed-hours-today** (e.g. 3h). Numerator window ≠ denominator window → rate is
       hugely overstated early in the day → `proj` explodes → the agent over-HOLDs and
       never marks down.
   - The synthetic path (`adapters/inventory.py::sell_through`) correctly returns
     cumulative-since-T0 units AND a rate over a **separate** trailing window — so live and
     synthetic compute **different quantities**, and the engine behaves inconsistently
     depending on which path fires.
   - **Action:** pin the definition (units since today's T0; rate over a short trailing
     window aligned to `q0`'s window), change the live OUTWARDED read to count from **T0**
     not `now−24h`, compute `run_rate` over a real trailing window (not `sold/elapsed`), and
     **add a unit test** validating both paths against a known day. This gates trusting any
     live decision.

2. **OUTWARDED sell-through too slow for busy SKUs** *(escalation blocker)*
   - `adapters/inventory.py::live_units_sold` times out (~30s) for high-volume sellers
     (Coriander/Spinach/Methi/Mint/Curry); only slow movers (e.g. Neem) return in time.
   - On timeout it silently falls back to the **synthetic curve** with `low_confidence=true`
     (`activities/pipeline.py::fetch_sellthrough`).
   - Root cause: OUTWARDED query has **no time index** server-side.
   - **Action:** escalate to SCM for a time-indexed query, or switch sell-through to a
     **Golden Eye POS feed**. Until then, busy SKUs are running synthetic.

3. **No trustworthy live Q0 / opening stock**
   - `activities/pipeline.py::plan_run` derives Q0 synthetically (`q0 = 30 + hash`).
   - Active `leftQty` is a stale 3-year lot pile; count ignores `createdTimeAfter`.
   - **Action:** obtain a clean opening-stock / GRN feed from SCM, wire into `plan_run`.
   - (Note: Q0's window must match the sell-through window — see P0 #1.)

4. **No real price anchor**
   - API `listingSellingPrice` returns ₹1 (unusable); `adapters/catalog.py` list_price/mrp
     are PLACEHOLDERS (`*` in UI). The whole ladder is computed off this anchor.
   - **Action:** source real list price/MRP from Golden Eye (or catalog/master API).

5. **No real category / shelf-life / expiry**
   - Curated in `adapters/catalog.py`; `ReceiptContext.expiry_date` is faked to receipt_date.
   - Eligibility + the "must clear today" L=1 gate depend on these.
   - **Action:** source from catalog/master API; without expiry the same-day gate is assumed.

6. **Catalog is a curated 9-JPIN stub**
   - `adapters/catalog.py::_CATALOG`; no catalog/master API. Blocks scaling past the
     hand-picked pilot SKUs.

---

## P1 — My J24 approval round-trip + write-path clients (required before any real price write)

These currently log/confirm without doing anything. A prod run that "succeeds" today
applies **no real price** and notifies **no real owner**. The approval loop today is
simulated entirely through the **internal React control plane**, not the real My J24 app.

### My J24 approval round-trip *(the gap the team flagged)*

Today's loop (`workflows/markdown.py`): on a step that `requires_approval`, the workflow
calls `request_owner_approval` → `notify.push_approval_card` (logs only), then **blocks**
on `wait_condition` for the `owner_decision` signal. That signal is delivered by the
**internal** endpoint `POST /api/runs/{run_id}/decision` (`api/main.py`), which the React
app calls — there is **no My J24 involvement**. On approve, the workflow runs
`apply_price_goldeneye` then `publish_offer`.

For prod, three pieces are missing:

  - **6a. Outbound: notify My J24 for approval.** Replace the `adapters/notify.py` stub with
    a real push to `notification.prod.jumbotail.com` so the owner gets the approval card in
    the My J24 app (price from→to, units left, reason).
  - **6b. Inbound: approval-callback API — DOES NOT EXIST YET.** When the owner approves in
    the My J24 app, My J24 must call **our** API to deliver the decision. The current
    `/api/runs/{run_id}/decision` is the internal web endpoint (no auth, run_id-keyed) and
    is **not** a My J24-facing callback. Build a new authenticated webhook/callback endpoint
    that maps a My J24 approval payload → the correct run → the `owner_decision` signal
    (`handle.signal("owner_decision", …)` via `_signal` in `api/main.py`). Needs auth,
    idempotency, and a card-id↔run_id mapping.
  - **6c. On approval → publish the prices.** Once the signal lands, the workflow already
    fans out to `apply_price_goldeneye` (Golden Eye write) and `publish_offer` (retail
    media). Those sinks are still stubs (items 7–8) — so "publish prices" is wired in the
    workflow but writes nothing real until 7 & 8 land.

### 7. Golden Eye price write
`adapters/goldeneye.py::apply_price` always returns `True` without writing. Idempotency/ledger
already handled in `db/repo.record_price_change` (keyed on run_id+rung), so only the real
HTTP write needs wiring. This is the "publish the price" sink from 6c.

### 8. Retail media / POS second screen
`adapters/retailmedia.py::publish_offer` is a stub; needs the real in-store screen publish
(the second sink from 6c).

### 9. LLM copy
`adapters/copy_llm.py` is a deterministic template (LLM disabled). Decide whether prod
enables a real LLM (Claude) for copy or ships the template. **Pricing must stay LLM-free** —
copy only (per the determinism rule in CLAUDE.md).

---

## P2 — Infra / ops hardening for prod

10. **DB has no migrations** — `db/` uses `init_db()` auto-create (per CLAUDE.md, "no
    migrations"). Prod needs a real migration path (Alembic, like the sibling `j24-pulse`).
11. **CORS wide open** — `api/main.py` uses CORS `*`. Lock to the real web origin.
12. **Temporal Cloud config** — confirm `TEMPORAL_API_KEY` / address / namespace wired for
    prod (code path exists, no change needed per README); verify worker/api connect.
13. **Secrets / `.env`** — live Bolt creds currently in `.env`. Move to a real secret store
    for prod; confirm `.env.example` documents every required var.
14. **Shadow mode as the rollout gate** — verify `shadow_mode` (log recommendations, no
    price writes) is the default first prod posture before enabling real Golden Eye writes.

---

## P3 — Testing, observability, release hygiene

15. **Test coverage is decision-engine only** — `tests/` covers `pricing/decision_engine.py`
    determinism. Add: live-adapter integration tests, the OUTWARDED timeout→fallback path,
    and a full workflow E2E (`WorkflowEnvironment.start_time_skipping()` + SQLite, per CLAUDE.md).
16. **Observability** — surface `low_confidence` / synthetic-fallback rate so ops can see
    when the agent is flying blind on busy SKUs; alert on Golden Eye write failures.
17. **Uncommitted code** — the entire live-Bolt integration is **uncommitted** on top of the
    single commit `fa97d89`. Commit/branch and get it under review before more work lands.

---

## Suggested sprint sequencing

- **Sprint goal = trustworthy reads on the BTM pilot SKUs:** P0 #1 (fix & test the
  sell-through rate math — top priority, even "live" is currently suspect) + P0 #2 (escalate
  OUTWARDED / evaluate POS feed) + P0 #3 #4 (Q0 + price feed). Nothing downstream is real
  until these land.
- **Then enable the approval round-trip in shadow:** P1 #6a (notify My J24) + #6b (build the
  inbound approval-callback API — does not exist yet) + #6c→#7 #8 (publish prices on
  approval), all gated behind `shadow_mode` (P2 #14) so no real price writes until validated.
- **Parallel track (no dependency):** P2 #10–#13 ops hardening + P3 #17 commit hygiene.
- **Before GA:** P3 #15 #16 tests + observability, then flip shadow off for the pilot.

## Verification (per item, when implemented)

- Reads: `python starter.py --store BTMLayout --jpin <JPIN> --speed 1800` and confirm
  `SellThrough.low_confidence=false` + real numbers in the Dashboard "Sold (24h)" column.
- Writes: run in `shadow_mode` first (ledger records, no Golden Eye write), then enable and
  confirm idempotency on `(run_id, rung)` via `db.repo`.
- Full stack: `docker compose up --build`; unit: `PYTHONPATH=. pytest tests/`.
