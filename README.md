# Perishable Offers — Same-Day Clearance (Temporal markdown agent)

A durable, day-long **Temporal** agent that clears short-shelf-life perishables.
One workflow run owns a single perishable batch: it watches sell-through through
the day and steps the price down a discrete ladder (**list → 25% → 50% → ₹1**),
asking the store owner for approval, applying the price via Golden Eye, and
publishing the offer to the in-store screens. The pricing is **deterministic**;
the LLM only shapes copy and never touches the price.

See `docs/Perishables-Markdown-Temporal-Design.md` for the full design + critique.

## Architecture

```
React/Vite (web)  →  FastAPI (api)  →  Temporal  →  Worker (PerishableMarkdownWorkflow)
                          │                              │
                          └──────────  Postgres  ────────┘   (read-model + price ledger)
```

- **`workflows/markdown.py`** — the orchestration: ladder timers, owner-decision
  signals, `current_state` query. Pure; all I/O is in activities.
- **`pricing/decision_engine.py`** — the deterministic `decide()` (hold vs step),
  unit-tested in `tests/`.
- **`activities/` + `adapters/`** — every external call (Inventory Item Details
  API, Golden Eye, My J24, retail media, LLM) behind a Protocol with a
  **stub backed by synthetic data**, so the loop runs end-to-end with no real
  Jumbotail services. Swap a stub for the real client to go live.
- **`db/`** — Postgres read-model + idempotent price ledger for the React app.
- **`api/main.py`** — control plane: list/inspect runs, seed demo runs, send
  owner decisions / overrides / GRN / sold-out signals.
- **`web/`** — Dashboard, Run detail (ladder + reason trail), Approvals queue, Config.

## Run it (Docker — self-contained)

```bash
cp .env.example .env
docker compose up --build
```

Brings up Postgres, a Temporal dev cluster (+ Temporal UI), the worker, the API,
and the web app:

| Surface | URL |
|---|---|
| Control-plane web app | http://localhost:8080 |
| FastAPI docs | http://localhost:8000/docs |
| Temporal UI | http://localhost:8081 |

In the web app: **Seed runs** → watch batches walk the ladder (a 13-hour day
replays in ~30s at the default clock speed). Approve a step from **Approvals**;
an RTE line auto-clears to ₹1 at close without a card. Toggle **shadow mode** to
log recommendations with no price writes.

## Run it (local, no Docker)

```bash
uv pip install -e .            # or: pip install temporalio fastapi uvicorn sqlalchemy "psycopg[binary]" pydantic
temporal server start-dev      # terminal 1
python worker.py               # terminal 2
uvicorn api.main:app --reload  # terminal 3  (:8000)
cd web && npm install && npm run dev   # terminal 4  (:5173)
```

CLIs: `python starter.py --store BTMLayout --jpin JPIN-CORIA-003 --speed 1800`,
`python approve.py --workflow <id> --approve`, `python schedule.py --store BTMLayout`.

## Temporal Cloud

Set `TEMPORAL_API_KEY` (+ the Cloud `TEMPORAL_ADDRESS` / `TEMPORAL_NAMESPACE` from
`.env.example`) and the worker/api connect to Cloud over the Namespace Endpoint
with API-key auth — no code change.

## Tests

```bash
PYTHONPATH=. pytest tests/      # decision-engine determinism guarantee
```

---

## v3 — profile-aware projection + shared read-model

v3 fixes the two things that blocked trustworthy live pricing and adds a test
harness, all additive and behind config flags:

- **Projection** — replaces flat `rate × remaining_h` with a profile-aware pace
  method (`pricing/projection.py` + `decide_v3`): observed sell-through sets the
  *level*, the hourly demand `share` curve sets the *shape*, so the agent holds
  through the 4 pm trough before the evening peak instead of over-marking-down.
  The `share` curve comes from a **separate** hourly-forecast workflow (not in
  this repo) that publishes to S3; we read it via `INTRADAY_PROFILE_S3_URI`
  (`adapters/profile.py`, synthetic fallback if unset).
- **Read-model** — `FacilitySellThroughPoller` (one per facility) batch-refreshes
  a `sell_through_snapshot`; batches read it in ~1 ms instead of each scanning the
  slow OUTWARDED endpoint (`READ_FROM_SNAPSHOT=true` + `python start_poller.py`).
- **Testing** — `tools/mock_bolt.py` is a stateful, evening-peaked mock gateway so
  the ladder actually walks with no real creds.

Flags: `PROJECTION_MODE=v3|v2`, `READ_FROM_SNAPSHOT`, `INTRADAY_PROFILE_S3_URI`
(or local `INTRADAY_PROFILE_PATH`; synthetic fallback if unset). Full
walkthrough: **`docs/V3-TESTING.md`**.
Design + diagram: **`docs/V3-Architecture-Design.md`** / `.docx`,
`docs/architecture-v3.svg`.

```bash
PYTHONPATH=. pytest tests/            # 39 tests, incl. the evening-peak proof
uvicorn tools.mock_bolt:app --port 9099   # then point the app at it (see V3-TESTING.md)
```

---

## Dead-stock multi-day clearance (separate workflow + UI)

Beyond same-day perishables, slow-moving / dead stock gets a **multi-day** markdown
ramp keyed to remaining shelf life, in its own workflow and a **Dead Stock** UI page.

- **Detect** — posgateway `POST /api/recommendation/dead-stock/{store}` (same source
  as `j24-pulse`; `adapters/deadstock.py`). Set `POSGATEWAY_BASE_URL` + `POSGATEWAY_TOKEN`.
- **Enrich** — on-hand / received-date / list price from Bolt; **`shelf_life_days` from a
  SKU-master parquet** (`adapters/sku_master.py`, `SKU_MASTER_S3_URI`/`SKU_MASTER_PATH`).
  When received/expiry is unknown, remaining runway is estimated from the
  **received-at-half-shelf-life** assumption.
- **Decide** — pure `pricing/deadstock_engine.py` → delegates to the existing
  `plan_clearance` ramp (escalating discount as expiry nears; clears to floor at terminal).
- **Run** — `DeadStockDiscoveryWorkflow` (per store, daily) discovers + optionally fans
  out `DeadStockClearanceWorkflow` (per SKU, one markdown/day, owner-approved,
  `continue_as_new`). Start with `python start_deadstock.py --store <id> [--auto-start]`.

Reuses the same task queue, Golden Eye apply, approval, standing-rule, and sim mode.

```bash
PYTHONPATH=. pytest tests/                       # unit + adapters (E2E auto-skips)
RUN_E2E=1 PYTHONPATH=. pytest tests/test_workflows_e2e.py   # time-skip workflow E2E
```
The E2E cases (`tests/e2e/`) run the real workflow → activity → SQLite path under a
Temporal test server, faking only the single live-Bolt activity: `sim_clearance_e2e`
proves the same-day v3 ladder walks; `deadstock_clearance_e2e` proves the multi-day
ramp clears to floor. They're opt-in (`RUN_E2E=1`) since they download a test-server
binary on first run.
