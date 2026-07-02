# Same-Day Perishables Clearance — v3 Architecture & Design

**Status:** proposed · builds on the shipped v2 repo · **Scope:** leafy greens, L=1, BTM Layout pilot → facility rollout

This document proposes the target architecture for the markdown agent and ships the
implementation of its three load-bearing changes. It is deliberately *evolutionary*:
v2 already made the hard calls correctly (Temporal per-batch ownership, a pure
deterministic price engine, a per-activity I/O boundary). v3 keeps all of that and
fixes the two things that actually block trustworthy live pricing — the **projection
model** and the **data-acquisition topology** — plus a **testing harness** that lets
you exercise the whole loop before any real integration lands.

---

## 1. The three decisions

| # | Question you asked | Decision | Why |
|---|---|---|---|
| 1 | Poll into a DB and have a *separate* workflow act on it, or one workflow? | **Decisions stay in one workflow per batch. Data *acquisition* is decoupled into a shared per-facility poller + read-model.** | Splitting the *actor* out re-creates the coordination problems Temporal removes (who acts, double-action, correlating a price change to its trigger). But splitting the *reader* out is pure upside: it protects the slow OUTWARDED endpoint and kills N× duplicated scans. Two different seams — cut the second, not the first. |
| 2 | Simulate with a Postman mock? | **Yes for contract testing; add a stateful mock for trajectory testing.** | The repo is already env-flag driven (`BOLT_BASE_URL`, `INVENTORY_SOURCE`) so a Postman mock validates HTTP shape + retry for free. But static responses don't move, so the ladder never walks. A tiny stateful fake that synthesizes an evening-peaked curve makes the price actually step. |
| 3 | Is sell-through logic fine, or use the hourly forecast? | **Neither alone — combine them. Observed sell-through supplies the *level*; the hourly profile supplies the *shape*.** | v2's flat `rate × remaining_h` extrapolation is wrong for greens specifically: demand is evening-peaked, so a 4pm midday rate understates what's coming and the agent over-marks-down right before the rush. A **separate** hourly-forecast workflow (not in this repo) produces exactly the shape needed and publishes it to S3; this repo only *reads* that artifact. |

---

## 2. Target architecture

```
              ┌────────────────────────── morning schedule (per facility) ──────────────────────────┐
              │  discover L=1 leafy candidates → start ONE FacilitySellThroughPoller + one           │
              │  PerishableMarkdownWorkflow per (store, jpin, receipt_date)                           │
              └──────────────────────────────────────────────────────────────────────────────────────┘
                                   │                                              │
        ┌──────────────────────────▼───────────────────┐        ┌────────────────▼───────────────────────────┐
        │  FacilitySellThroughPoller  (1 / facility)    │        │  PerishableMarkdownWorkflow (1 / batch)      │
        │  durable timer → poll_facility_snapshot       │        │  observe → decide_v3 → ask → apply → learn   │
        │  (ONE batched OUTWARDED scan for all K JPINs)  │        │                                              │
        └──────────────────────────┬───────────────────┘        │  each tick:                                  │
                                   │ writes                       │   read_snapshot ─────────────┐               │
                                   ▼                              │   resolve_intraday_profile ──┤ (both activities)│
                       ┌───────────────────────┐                  │   project_remaining_demand ──┤ (pure)         │
                       │  sell_through_snapshot │◀─── reads ───────┘   decide_v3 ─────────────────┘ (pure)         │
                       │  (Postgres read-model) │                  │   → apply_price_goldeneye (idempotent)        │
                       └───────────────────────┘                  └──────────────────────────────────────────────┘
                                                                          │ reads shape from
                                                                          ▼
                                                        ┌──────────────────────────────────┐
                                                        │ intraday profile artifact         │
                                                        │ (external hourly wf → S3 share    │
                                                        │  curve; this repo only reads it)  │
                                                        └──────────────────────────────────┘
```

**Read path (the change):** batch workflows no longer scan Bolt on every tick.
The poller does one batched scan per facility per interval and writes the snapshot;
each batch reads the snapshot in ~1 ms, with a direct-read fallback if it's stale.
For K candidate JPINs and N batches this turns **O(N·K)** slow scans per tick into
**O(K)** batched scans — and it's the concrete fix for the OUTWARDED-latency P0.

**Decision path (unchanged ownership):** every markdown decision still lives in the
per-batch workflow. The poller *never* decides; it only refreshes data. Correlation
between a price change and the reading that caused it — and the replay-based audit
trail — is fully preserved.

---

## 3. The projection model (the core fix)

v2 projected end-of-day clearance with a flat extrapolation:

```
proj = units_sold + recent_rate × remaining_h        # assumes the rate holds flat
```

v3 replaces the `recent_rate × remaining_h` term with a **blended, profile-aware
estimate of remaining demand** (`pricing/projection.py`), then feeds that into the
same price policy (`decide_v3`). Two estimators:

- **RATE** — `recent_rate × remaining_h`. Robust at open (little has sold), blind to
  the day's shape.
- **PACE** — infer today's *level* from what has already sold, using the profile's
  elapsed share, then project the remainder by the profile's remaining share:

  ```
  D_hat          = units_sold / cum_share_to_now      # today's total demand, implied
  remaining_pace = D_hat × remaining_share            # carries the evening peak
  ```

They are blended by a weight `w` that grows with **both** how much of the day's demand
has elapsed **and** how many units have actually been observed — so v3 leans on RATE at
open (pace is noisy when `cum_share ≈ 0`) and on PACE from mid-morning onward:

```
w                 = min( clip(cum_share/share_ref), clip(units_sold/units_ref) )
remaining_demand  = w × remaining_pace + (1−w) × remaining_rate
```

The elasticity lift for candidate cuts scales `remaining_demand`, exactly where v2
scaled `recent_rate`. Everything else — price floor, smallest-cut search, hysteresis,
RTE token auto-clear, monotonic non-increasing price — is untouched.

**Worked example (the leafy-greens trap), from the unit tests:**
Coriander, Q0 = 60, sold 27 by 16:00, midday rate 3/hr, 5 h nominally left.

- **v2 flat:** `27 + 3×5 = 42 of 60` → *behind* → **marks down** right before the rush.
- **v3 pace:** profile says 45% of the day has happened, 55% is ahead. `D_hat = 27/0.45 = 60`, `remaining = 60×0.55 = 33`, `proj = 60 of 60` → **HOLD**, margin protected.

And the guardrail test: a *genuine* laggard (sold 8 of 60 by 4pm) still steps — the
profile can't clear it even with the peak, so v3 marks it down. The profile stops
over-discounting; it never stops discounting real slow movers.

**Shape, not level.** v3 uses only the profile's `share` curve — never the absolute
`hourly_units` from a daily point forecast. That keeps it robust to a stale/missing
daily forecast: same-day sell-through self-corrects the level for weather, festivals,
a bad batch. Getting the *shape* working is the high-value, cheap win; the daily
point-forecast join is a later nice-to-have, not a dependency.

---

## 4. Determinism is preserved

The determinism boundary that makes the audit trail free is not weakened. Both new
inputs arrive as **plain parameters** computed off the workflow's own deterministic
clock and resolved in activities:

- `resolve_intraday_profile` (activity) reads the artifact and returns `cum_share_to_now`
  / `remaining_share` — I/O stays out of the workflow.
- `project_remaining_demand` and `decide_v3` are **pure**: no clock, no I/O, no
  randomness. Same inputs → same projection (there's a test asserting exactly this).

The workflow computes the nominal IST `hour`/`dow`/`frac` from `workflow.now()` and its
elapsed-time math (already deterministic today) and passes them to the profile activity.

---

## 5. Workflow integration (the one file to wire by hand)

The per-batch loop in `workflows/markdown.py` changes in three small places. Sketch:

```python
# (a) read sell-through from the shared snapshot instead of scanning Bolt directly
st = await workflow.execute_activity(
    read_snapshot,
    args=[store_id, jpin, receipt_date, self._state.q0],
    **_READ,
)

# (b) resolve the intraday demand shape for this moment (deterministic hour/dow/frac)
nominal_hour_ist = _T0_HOUR_IST + nominal_elapsed_h
prof = await workflow.execute_activity(
    resolve_intraday_profile,
    args=[store_id, jpin, _dow(receipt_date), int(nominal_hour_ist),
          nominal_hour_ist % 1.0, _T0_HOUR_IST, cfg.store_close_hour],
    **_READ,
)

# (c) project (pure) then decide with v3 (pure)
proj = project_remaining_demand(
    units_sold=st.units_sold_today, recent_rate=st.recent_rate,
    remaining_h=remaining_h,
    cum_share_to_now=prof.cum_share_to_now, remaining_share=prof.remaining_share,
    profile_source=prof.source_level,
)
decision = decide_v3(
    q0=self._state.q0, units_sold=st.units_sold_today,
    remaining_demand=proj.remaining_demand,
    current_price=self._state.current_price, list_price=r.list_price,
    floor_price=plan.floor_price, elasticity=cfg.elasticity,
    token_free_price=cfg.token_free_price, residual_tolerance=cfg.residual_tolerance,
    step_pct=cfg.step_pct, max_discount_pct=cfg.max_discount_pct,
    hysteresis_units=cfg.hysteresis_units, is_rte=r.is_rte,
    past_rte_gate=past_rte_gate, token_eligible=token_eligible,
    projection_method=proj.method,
)
```

`decide_v2` and the old `fetch_sellthrough` stay in the tree, so this can ship behind a
config flag (`PROJECTION_MODE=v3|v2`) and be shadow-compared before cutover.

Register the new workflow + activities on the worker:

```python
Worker(client, task_queue=TASK_QUEUE,
       workflows=[PerishableMarkdownWorkflow, FacilitySellThroughPoller],
       activities=[..., resolve_intraday_profile, poll_facility_snapshot, read_snapshot])
```

---

## 6. Testing strategy (three layers)

| Layer | Target | Tool | What it proves |
|---|---|---|---|
| **Unit (pure)** | projection + price policy | `tests/test_projection.py` | evening-peak HOLD vs flat STEP, laggard still steps, early-day leans on rate, determinism |
| **Contract** | Bolt HTTP client | Postman static mock → `BOLT_BASE_URL` | headers, `createdTimeAfter`, 5xx retry/backoff, JSON shape |
| **Trajectory** | the whole loop | `tools/mock_bolt.py` (stateful) → `BOLT_BASE_URL` | sell-through *rises* through the day, projection updates, ladder actually walks, snapshot poller + batch read wired |

Run trajectory locally:

```bash
uvicorn tools.mock_bolt:app --port 9099          # evening-peaked, time-moving
BOLT_BASE_URL=http://localhost:9099 INVENTORY_SOURCE=live BOLT_AUTH_TOKEN=mock \
  MOCK_SPEED=1800 python worker.py                # 13h day in ~30s
```

The mock keys synthesized OUTWARDED counts off the request's `createdTimeAfter`, so the
trailing-window rate and the since-T0 cumulative both come out consistent — which also
exercises the snapshot poller's two-window read.

---

## 7. File manifest

**New**
- `pricing/projection.py` — pure profile-aware projector (`project_remaining_demand`, `DayProjection`).
- `activities/profile.py` — activity resolving the intraday profile.
- `adapters/profile.py` — reads the hourly `share` artifact from S3 (`INTRADAY_PROFILE_S3_URI`, via `adapters/_s3.py`; local `INTRADAY_PROFILE_PATH` override for tests); evening-peaked synthetic fallback.
- `adapters/_s3.py` — tiny read-only S3 helper (parse URI, ETag version, cache download). boto3 lazy-imported.

> **Out of scope for this repo:** the hourly-forecast pipeline that *produces* the
> `share` artifact runs as a separate workflow and publishes to S3. This repo only
> consumes its output.
- `activities/snapshot.py` — batched facility poll + fast `read_snapshot` (with direct-read fallback).
- `workflows/facility_poller.py` — `FacilitySellThroughPoller` (one per facility, continue-as-new).
- `tools/mock_bolt.py` — stateful Bolt mock for trajectory testing.
- `tests/test_projection.py` — v3 unit tests (all passing).

**Changed (additive, non-breaking)**
- `pricing/decision_engine.py` — added `decide_v3` (projection-driven; `decide_v2` untouched).
- `shared/models.py` — added `IntradayProfile`, `AddJpinsRequest`.
- `db/models.py` — added `SellThroughSnapshotRow` read-model.
- `db/repo.py` — added `upsert_sell_through_snapshot` / `get_sell_through_snapshot`.

**To wire by hand**
- `workflows/markdown.py` — swap in `read_snapshot` + `resolve_intraday_profile` + `decide_v3` (§5), behind `PROJECTION_MODE`.
- `worker.py` — register the new workflow + activities.
- `schedule.py` — start one `FacilitySellThroughPoller` per facility alongside the batch runs.

---

## 8. Rollout

1. **Shadow v3** — run `decide_v3` in shadow next to `decide_v2`, log both decisions, no writes. Compare where they diverge (expect: v3 holds through the evening trough that v2 marks down).
2. **Snapshot poller live, batches still direct** — stand up the poller, verify snapshot freshness/coverage, keep batches reading Bolt directly. De-risks the read-model independently.
3. **Cut batches to `read_snapshot`** — flip the read path; the built-in staleness fallback protects against poller lag.
4. **Enable v3 pricing** for a subset of JPINs, then the facility, once the profile artifact is being published daily and the shadow deltas look right.

Each step is independently reversible via a config flag; none requires a schema migration
beyond the additive `sell_through_snapshot` table (auto-created by `init_db()`).
