# J24 Same-Day Perishables Clearance — Architecture & Technical Reference

## 1. Purpose and requirements

### Business problem
J24 Essentials stores receive short-shelf-life perishables (leafy greens, L=1 day) each morning. Stock that doesn't sell by store close becomes write-off waste. The system automates intraday price markdowns to maximise sell-through while giving store owners control over the price steps.

### Functional requirements
- One durable workflow run per perishable batch (store × JPIN × receipt date) for the entire day.
- Walk a discrete price ladder: **list price → −25% → −50% → ₹1 token** at configurable time/clock triggers.
- Fetch live sell-through from the Inventory Item Details API at every checkpoint.
- Show a markdown recommendation with reason (hold / step / auto-clear) at each rung.
- Require owner approval before applying a price step; auto-clear RTE (Ready-to-Eat) lines to ₹1 at store close without consent.
- Write the confirmed price via Golden Eye and publish an offer headline to in-store screens.
- Emit an immutable per-checkpoint audit record.
- Accept mid-run signals: owner decision, additional GRN (stock rebasing), sold-out, manual override.
- Support shadow mode: record recommendations without writing any prices.
- Support `demo_speed` to replay a 13-hour day in seconds for demos.

### Non-functional requirements
- **Determinism / auditability**: the price decision must be 100% reproducible from logged inputs.
- **Durability**: a worker crash mid-day must not lose state — Temporal handles replay.
- **Idempotency**: price writes are idempotent on `(run_id, rung)`.
- **Observability**: a React control plane shows live run state; Postgres gives a queryable read-model.
- **Extensibility**: all external dependencies are behind Protocol-style adapter stubs — swap a stub for the real client to go live.

---

## 2. Tech stack

| Layer | Technology | Version |
|---|---|---|
| Workflow engine | [Temporal](https://temporal.io) | `temporalio >= 1.8.0` |
| API server | [FastAPI](https://fastapi.tiangolo.com) | `>= 0.110` |
| ASGI server | Uvicorn (standard) | `>= 0.29` |
| ORM / DB | SQLAlchemy 2.0 + psycopg 3 | `>= 2.0`, `>= 3.1` |
| Database | PostgreSQL | any recent version |
| Data validation | Pydantic v2 | `>= 2.0` |
| HTTP client | HTTPX (async) | `>= 0.27` |
| Frontend | React 18 + Vite + TypeScript | — |
| Runtime | Python ≥ 3.10 | — |
| Containerisation | Docker Compose | — |

---

## 3. System architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│  Browser                                                            │
│  React/Vite (web/ · :8080)                                          │
│   Dashboard · Run detail · Approvals queue · Config picker          │
└───────────────────────────┬─────────────────────────────────────────┘
                            │ REST (CORS *)
┌───────────────────────────▼─────────────────────────────────────────┐
│  FastAPI control plane (api/main.py · :8000)                        │
│  • seed runs       • send signals (decision / override / GRN / sold)│
│  • list / inspect  • live state query via Temporal RPC              │
└─────────────────┬───────────────────────┬───────────────────────────┘
                  │ Temporal Client       │ SQLAlchemy
┌─────────────────▼─────────┐  ┌─────────▼──────────────────────────┐
│  Temporal Cluster          │  │  PostgreSQL                         │
│  (task queue: perishables-tq│  │  read-model + price ledger +        │
│   or Temporal Cloud)       │  │  audit trail (no migrations —       │
└─────────────────┬─────────┘  │  auto-created on start)             │
                  │            └────────────────────────────────────┘
┌─────────────────▼─────────────────────────────────────────────────┐
│  Worker  (worker.py)                                               │
│  PerishableMarkdownWorkflow (workflows/markdown.py)                │
│  ┌──────────────────────────────────────────────────────────────┐ │
│  │  Checkpoint loop (Temporal durable timers)                   │ │
│  │  ┌─────────────────────────────────────────────────────────┐ │ │
│  │  │  pricing/decision_engine.py::decide()  ← pure function  │ │ │
│  │  └─────────────────────────────────────────────────────────┘ │ │
│  │  Activities (all I/O / side effects)                         │ │
│  │   plan_run · fetch_sellthrough · apply_price_goldeneye       │ │
│  │   request_owner_approval · shape_offer_llm · publish_offer   │ │
│  │   persist_state · persist_decision · write_audit             │ │
│  └──────────────────────────────────────────────────────────────┘ │
└─────────────────┬──────────────────────────────────────────────────┘
                  │  adapters (Protocol stubs → real clients)
     ┌────────────┼────────────────────────────────────────┐
     ▼            ▼            ▼            ▼              ▼
  Bolt Gateway  Golden Eye   My J24      Retail media    LLM
  (inventory)   (price write) (notify)   (second screen) (copy)
```

### Component responsibilities

| Component | File | Responsibility |
|---|---|---|
| Workflow | `workflows/markdown.py` | Pure orchestration — timers, signal handling, query, calls activities |
| Decision engine | `pricing/decision_engine.py` | Stateless `decide()` — hold / step / auto-clear; no I/O |
| Ladder config | `pricing/ladder.py` | Default rungs, thresholds — snapshotted at run start |
| Activities | `activities/pipeline.py` | All external I/O wrapped in `@activity.defn` |
| DB activities | `activities/persistence.py` | Write read-model and audit via `db/repo.py` |
| Adapters | `adapters/` | Thin stubs with synthetic data; real impls swap in here |
| Bolt client | `adapters/_bolt.py` | Only file that does HTTP to the Inventory Gateway |
| Shared models | `shared/models.py` | Pydantic types crossing the Temporal boundary |
| DB models | `db/models.py` | SQLAlchemy ORM tables |
| DB repo | `db/repo.py` | All reads/writes via short-lived sessions |
| FastAPI app | `api/main.py` | Control plane REST endpoints |
| Store directory | `shared/stores.py` | ~60 J24 stores with bzid / facility / org ids |
| React app | `web/src/` | Dashboard, run detail, approvals, config |

---

## 4. Determinism boundary

The hard architectural rule: **workflows orchestrate; activities do I/O.**

```
┌────────────────────────────────────────────────────────────────┐
│  DETERMINISTIC ZONE (workflows/markdown.py)                    │
│  • workflow.now()  — never datetime.now()                      │
│  • workflow.wait_condition(..., timeout=...)  — all timers     │
│  • calls decide() in-process (pure function, no I/O)           │
│  • config snapshotted at run start via plan_run activity       │
│  • no network, no DB, no secrets, no randomness                │
└───────────────────────────┬────────────────────────────────────┘
                            │ execute_activity(...)
┌───────────────────────────▼────────────────────────────────────┐
│  SIDE-EFFECT ZONE (activities/)                                │
│  • real clock (time.time())                                    │
│  • network (Bolt Gateway, Golden Eye, notifications)           │
│  • database reads/writes                                       │
│  • LLM calls                                                   │
└────────────────────────────────────────────────────────────────┘
```

`pricing/decision_engine.py::decide()` is a pure function — given the same inputs it always returns the same output. It receives all time values as parameters. This is the audit guarantee and is verified by unit tests.

---

## 5. Markdown ladder

### Default rungs (`pricing/ladder.py`)

```python
DEFAULT_RUNGS = [
    RungDef(index=0, label="R0", elapsed_hours=0.0,  wallclock_hour_ist=None, ceiling_pct=0.0,   token_free=False),
    RungDef(index=1, label="R1", elapsed_hours=2.0,  wallclock_hour_ist=None, ceiling_pct=25.0,  token_free=False),
    RungDef(index=2, label="R2", elapsed_hours=8.0,  wallclock_hour_ist=16,   ceiling_pct=50.0,  token_free=False),
    RungDef(index=3, label="R3", elapsed_hours=None, wallclock_hour_ist=21,   ceiling_pct=100.0, token_free=True),
]
```

Each checkpoint fires on whichever trigger (elapsed hours or IST wall-clock) comes **first**. Checkpoints are pre-computed by `plan_run` as second offsets scaled by `demo_speed`, so the workflow only ever compares numbers.

| Rung | Nominal trigger | Price | Approval needed |
|---|---|---|---|
| R0 | T0 (08:00 IST) | list price | No (observe only) |
| R1 | +2 h | list × 0.75 | Yes |
| R2 | +8 h or 16:00 | list × 0.50 | Yes |
| R3 | 21:00 (store close) | ₹1 token | Yes — except RTE lines, which auto-clear |

### Decision algorithm (`pricing/decision_engine.py`)

```python
proj  = units_sold + run_rate × max(0, nominal_remaining_h)
ratio = proj / q0

if ratio >= 1.0:          → HOLD   (on track to clear)
elif ratio >= theta_hold: → STEP   (one rung toward ceiling)
else:                     → STEP   (jump straight to ceiling)

# RTE override: past the close gate → AUTO_CLEAR to ₹1, no approval
if ceiling_rung.token_free and is_rte and past_rte_gate:
    → AUTO_CLEAR
```

Price is **monotonic non-increasing**: the target is never below the current rung. `theta_hold` defaults to 0.85.

---

## 6. Workflow in detail

### Workflow ID and reuse
```
perish-markdown-{store_id}-{jpin}-{receipt_date}
Reuse policy: ALLOW_DUPLICATE  (safe to re-trigger the same batch)
Task queue:   perishables-tq
```

### Run lifecycle

```
plan_run (activity)
  → eligible?  No → log SKIPPED, return
  → Yes → MarkdownState initialised at R0

for checkpoint in plan.checkpoints:
    _sleep_until(checkpoint.sleep_offset_s)     # durable Temporal timer
    if stop or sold_out: break

    # honour pending GRN / force-rung signals
    fetch_sellthrough (activity)                # live OUTWARDED or synthetic
    decide()                                    # pure, in-process
    if STEP and requires_approval and not shadow:
        wait_condition(owner_decision, timeout=30 min)
    if apply:
        apply_price_goldeneye (activity)        # idempotent on (run_id, rung)
        shape_offer_llm (activity)              # offer copy headline
        publish_offer (activity)                # retail media
    persist_decision / write_audit (activities)
    persist_state (activity)                    # sync read-model

_finalize → status = FINALIZED | SOLD_OUT | STOPPED
```

### Activity retry/timeout matrix

| Preset | Timeout | Max attempts | Used for |
|---|---|---|---|
| `_READ` | 50 s | 3 | `plan_run`, `fetch_sellthrough` (OUTWARDED ladder is slow) |
| `_NOTIFY` | 30 s | 5 | `request_owner_approval`, `publish_offer`, `notify_owner` |
| `_APPLY` | 20 s | 10 | `apply_price_goldeneye` (price write must be confirmed) |
| `_LLM` | 8 s | 1 | `shape_offer_llm` (never block a markdown on copy) |
| `_DB` | 15 s | 5 | All DB persistence activities |

### Signals

```python
@workflow.signal  owner_decision(OwnerDecision)   # approve / reject a pending step
@workflow.signal  additional_grn(AdditionalGrn)   # re-baseline Q0 mid-day
@workflow.signal  sold_out()                       # finalize early
@workflow.signal  manual_override(ManualOverride)  # force_rung | stop
```

### Query

```python
@workflow.query   current_state() → MarkdownState | None   # live snapshot
```

---

## 7. Database structure

Tables are auto-created via `db.database.init_db()` (SQLAlchemy `create_all`, idempotent). No migrations.

### `stores`
| Column | Type | Notes |
|---|---|---|
| `store_id` | `VARCHAR(64)` PK | BZID, e.g. `BZID-1304298141` |
| `name` | `VARCHAR(128)` | Display name |
| `close_hour_ist` | `INTEGER` | Default 21 |
| `created_at` | `TIMESTAMPTZ` | — |

### `markdown_runs`
| Column | Type | Notes |
|---|---|---|
| `run_id` | `VARCHAR(160)` PK | Temporal workflow ID |
| `store_id` | `VARCHAR(64)` idx | — |
| `jpin` | `VARCHAR(64)` idx | — |
| `receipt_date` | `VARCHAR(16)` | ISO date |
| `clearance_date` | `VARCHAR(16)` | Same as receipt for L=1 |
| `product_title` | `VARCHAR(256)` | — |
| `category` | `VARCHAR(64)` | e.g. `FNV_LEAFY` |
| `is_rte` | `BOOLEAN` | Ready-to-Eat flag |
| `status` | `VARCHAR(32)` idx | `STARTED / OBSERVING / AWAITING_APPROVAL / APPLYING / FINALIZED / SOLD_OUT / STOPPED` |
| `current_rung` | `VARCHAR(8)` | `R0`–`R3` |
| `list_price` | `FLOAT` | Snapshot at run start |
| `current_price` | `FLOAT` | Last applied price |
| `q0` | `INTEGER` | Opening stock (may be rebased by GRN) |
| `units_sold` | `INTEGER` | Last observed |
| `awaiting_approval` | `BOOLEAN` idx | Drives the approvals badge |
| `shadow_mode` | `BOOLEAN` | Never writes prices when true |
| `summary` | `TEXT` | Last reason string |
| `created_at` / `updated_at` | `TIMESTAMPTZ` | — |

### `decisions`
| Column | Type | Notes |
|---|---|---|
| `id` | `INTEGER` PK | — |
| `run_id` | `VARCHAR(160)` idx | — |
| `rung` | `VARCHAR(8)` | Target rung at this checkpoint |
| `price` | `FLOAT` | — |
| `units_sold` | `INTEGER` | — |
| `run_rate` | `FLOAT` | Units / nominal hour |
| `projected_clearance` | `FLOAT` | — |
| `residual` | `FLOAT` | — |
| `ratio` | `FLOAT` | `proj / q0` |
| `decision` | `VARCHAR(16)` | `HOLD / STEP / AUTO_CLEAR` |
| `approval` | `VARCHAR(16)` | `NOT_REQUIRED / APPROVED / REJECTED / TIMEOUT_HOLD` |
| `reason` | `TEXT` | One-line human-readable explanation |
| `ts` | `TIMESTAMPTZ` | — |

### `price_changes` (durable ledger)
| Column | Type | Notes |
|---|---|---|
| `id` | `INTEGER` PK | — |
| `run_id` | `VARCHAR(160)` idx | — |
| `store_id` / `jpin` | `VARCHAR` idx | — |
| `rung` | `VARCHAR(8)` | — |
| `from_price` / `to_price` | `FLOAT` | — |
| `confirmed` | `BOOLEAN` | True = Golden Eye confirmed |
| `ts` | `TIMESTAMPTZ` | — |
| **UNIQUE** | `(run_id, rung)` | Idempotency constraint |

### `run_events`
| Column | Type | Notes |
|---|---|---|
| `id` | `INTEGER` PK | — |
| `run_id` | `VARCHAR(160)` idx | — |
| `kind` | `VARCHAR(32)` | `STARTED / APPLIED / AWAITING_APPROVAL / TIMEOUT_HOLD / GRN / SKIPPED / FINALIZED / SOLD_OUT / STOPPED` |
| `message` | `TEXT` | — |
| `ts` | `TIMESTAMPTZ` | — |

### `audit_events`
| Column | Type | Notes |
|---|---|---|
| `id` | `INTEGER` PK | — |
| `run_id` | `VARCHAR(160)` idx | — |
| `payload` | `TEXT` | Full `AuditEvent` serialised as JSON |
| `ts` | `TIMESTAMPTZ` | — |

### `offers`
| Column | Type | Notes |
|---|---|---|
| `id` | `INTEGER` PK | — |
| `run_id` | `VARCHAR(160)` idx | — |
| `rung` | `VARCHAR(8)` | — |
| `headline` | `VARCHAR(256)` | LLM-shaped offer copy |
| `price` | `FLOAT` | — |
| `channel` | `VARCHAR(32)` | `retail_media` |
| `ts` | `TIMESTAMPTZ` | — |

---

## 8. REST API (`api/main.py`)

Base URL: `http://localhost:8000`

### Utility

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/health` | Liveness check → `{"ok": true}` |
| `GET` | `/api/config` | Default ladder config (rungs, thresholds) |
| `GET` | `/api/stores` | Full J24 store directory (~60 stores) |

### Catalogue / inventory

| Method | Path | Query params | Description |
|---|---|---|---|
| `GET` | `/api/candidates` | `store_id` | Perishable JPINs selectable for this store |
| `GET` | `/api/inventory` | `store_id`, `hours` | Live units-sold snapshot per JPIN over the last N hours |

### Runs

| Method | Path | Body / params | Description |
|---|---|---|---|
| `GET` | `/api/runs` | — | All runs (summary list, newest first) |
| `GET` | `/api/runs/{run_id}` | — | Run detail + events + decisions + price changes + offers + live Temporal state |
| `GET` | `/api/runs/{run_id}/audit` | — | Immutable audit trail for a run |
| `POST` | `/api/runs/seed` | `SeedRequest` | Start one or more markdown workflows |
| `POST` | `/api/runs/{run_id}/decision` | `OwnerDecision` | Approve or reject a pending price step |
| `POST` | `/api/runs/{run_id}/override` | `ManualOverride` | Force a rung or stop the run |
| `POST` | `/api/runs/{run_id}/grn` | `AdditionalGrn` | Rebase Q0 with additional received stock |
| `POST` | `/api/runs/{run_id}/soldout` | — | Mark the line as sold out |

#### `POST /api/runs/seed` — `SeedRequest`
```json
{
  "count": 3,
  "store_id": "BZID-1304298141",
  "shadow_mode": false,
  "demo_speed": 1800.0,
  "include_rte": true,
  "jpins": ["JPIN-1304597126", "JPIN-1304597236"]
}
```
`jpins` overrides `count` — pass the UI multi-select list directly. `demo_speed=1800` replays 1 nominal hour in 2 seconds.

#### `POST /api/runs/{run_id}/decision` — `OwnerDecision`
```json
{ "rung": "R2", "approve": true, "note": "ok to mark down" }
```

#### `POST /api/runs/{run_id}/override` — `ManualOverride`
```json
{ "action": "force_rung", "rung": "R3" }
{ "action": "stop" }
```

---

## 9. Inventory Item Details API integration

The only live external dependency is the **Bolt Gateway** (`adapters/_bolt.py`).

### Endpoints called

```
POST {BOLT_BASE_URL}/api/space/product/details/for-state-status-facility
POST {BOLT_BASE_URL}/api/space/product/count/for-state-status-facility
```

### Request shape

```json
{
  "jpins": ["JPIN-1304597126"],
  "facilityId": "FACIL-1441684082",
  "inventoryItemStates": ["OUTWARDED"],
  "inventoryItemStatuses": ["ACTIVE", "EXHAUSTED"],
  "createdTimeAfter": 1719600000000
}
```

Headers: `userId`, `orgId`, `Authorization: Bearer <jwt>`, `Content-Type: application/json`.

### Usage in the system

| Use case | States queried | Why |
|---|---|---|
| Listing price at run start | `SELLABLE / FULFILMENT / INWARDED / UNDER_TRANSFER` | `listingSellingPrice` from active rows |
| Sell-through per checkpoint | `OUTWARDED` | COUNT of outward movements = units sold (active `leftQty` does not decrement on sale) |
| Inventory snapshot (`/api/inventory`) | `OUTWARDED` | Same — fanned out per JPIN |

### OUTWARDED window ladder
The OUTWARDED scan is slow for high-volume JPINs. `fetch_sellthrough` tries windows widest-first and takes the first result that returns within the per-try budget:

```
Default ladder: 47 h → 36 h → 24 h
Budget: 36 s total, split across attempts
Ceiling: 47 h (gateway rejects createdTimeAfter > 48 h)
Fallback: synthetic sell-through curve with low_confidence=True
```

Configurable via `INVENTORY_SELLTHROUGH_WINDOWS_H=47,36,24`.

---

## 10. Adapters — live vs stub

All external integrations sit behind a thin adapter. Set `INVENTORY_SOURCE=live` and supply the Bolt credentials to enable the only currently-live integration; the rest remain as stubs.

| Adapter | File | Live state | How to activate |
|---|---|---|---|
| Inventory / Bolt Gateway | `adapters/inventory.py` + `adapters/_bolt.py` | **LIVE** | `INVENTORY_SOURCE=live` + `BOLT_*` env vars |
| Catalog | `adapters/catalog.py` | Stub | Replace `_CATALOG` with a real catalogue query |
| Golden Eye (price write) | `adapters/goldeneye.py` | Stub | Replace `apply_price()` with the real API call |
| Owner notifications | `adapters/notify.py` | Stub | Replace with My J24 push / FCM |
| Retail media | `adapters/retailmedia.py` | Stub | Replace `publish_offer()` with the second-screen API |
| LLM copy | `adapters/copy_llm.py` | Stub | Set `enable_llm=True` in `MarkdownConfig` |

### Synthetic sell-through curve (demo / tests)

When live reads are disabled or time out the adapter falls back to a deterministic JPIN-keyed curve:

```python
def _demand_strength(jpin: str) -> float:
    h = int(hashlib.sha256(jpin.encode()).hexdigest(), 16)
    return 0.45 + (h % 80) / 100.0   # 0.45 .. 1.24 — stable per JPIN

# < 1.0 → line needs a markdown; ≥ 1.0 → clears on its own
sold = q0 * demand_strength(jpin) * curve(frac_time) * demand_boost(markdown_pct)
```

---

## 11. Shared Pydantic models (`shared/models.py`)

All types that cross the Temporal boundary (workflow ↔ activities ↔ API) are defined here.

| Model | Description |
|---|---|
| `RungDef` | One step on the ladder (index, label, trigger, ceiling %) |
| `MarkdownConfig` | Full ladder + all thresholds — snapshotted at run start |
| `ReceiptContext` | Per-batch facts from the Inventory API |
| `SellThrough` | units sold + run rate from `fetch_sellthrough` |
| `Checkpoint` | Pre-planned ladder checkpoint (sleep offset in seconds) |
| `RunPlan` | `plan_run` output — receipt + config + checkpoints + eligibility |
| `DecisionResult` | Output of `decide()` — rung, price, decision, reason, approval flag |
| `MarkdownState` | Full run state exposed via the `current_state` query |
| `HistoryEntry` | One row in the state's history list |
| `AuditEvent` | Immutable checkpoint audit record written to the DB |
| `OwnerDecision` | Signal payload: approve / reject |
| `AdditionalGrn` | Signal payload: additional stock qty |
| `ManualOverride` | Signal payload: force_rung or stop |
| `SeedRequest` | API helper to start runs from the UI |

---

## 12. Frontend — React control plane (`web/src/`)

| View | Route | Features |
|---|---|---|
| Dashboard | `/` | Runs list (polling 2 s), seed dialog (store picker, JPIN multi-select, shadow/demo-speed), status badges |
| Run detail | `/runs/:id` | Live state panel, ladder progress, decision trail, event log, price ledger, approve/reject buttons |
| Approvals | `/approvals` | Queue of runs awaiting owner decision, badge count in nav |
| Config | `/config` | Ladder rungs, thresholds display |

The app polls `GET /api/runs` every 2 seconds; run detail fetches `GET /api/runs/{id}` which merges the Postgres read-model with a live Temporal query.

---

## 13. Running the system

### Docker (self-contained)

```bash
cp .env.example .env
docker compose up --build
```

| Surface | URL |
|---|---|
| React control plane | http://localhost:8080 |
| FastAPI docs (Swagger) | http://localhost:8000/docs |
| Temporal UI | http://localhost:8081 |

### Local (no Docker)

```bash
# 1. Install dependencies
uv pip install -e .

# 2. Start services (each in a separate terminal)
temporal server start-dev            # terminal 1 — local Temporal cluster
python worker.py                     # terminal 2 — workflow + activity worker
uvicorn api.main:app --reload        # terminal 3 — control plane (:8000)
cd web && npm install && npm run dev # terminal 4 — React app (:5173)
```

### Temporal Cloud

Set `TEMPORAL_API_KEY` (and the Cloud `TEMPORAL_ADDRESS` / `TEMPORAL_NAMESPACE` from `.env.example`). The worker and API connect to Cloud over the namespace endpoint with API-key auth — no code change required.

### Seeding a demo run

```bash
python starter.py --store BTMLayout --jpin JPIN-1304597126 --speed 1800
python approve.py --workflow perish-markdown-BZID-1304298141-JPIN-1304597126-2024-01-15 --approve
python schedule.py --store BTMLayout   # start all candidates for the store
```

`demo_speed=1800` compresses one nominal hour into 2 seconds → a 13-hour day replays in ~26 seconds.

---

## 14. Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | `postgresql+psycopg://perishable:perishable@localhost:5432/perishable` | PostgreSQL DSN |
| `TEMPORAL_ADDRESS` | `localhost:7233` | Temporal server or Cloud endpoint |
| `TEMPORAL_NAMESPACE` | `default` | Temporal namespace |
| `TEMPORAL_TASK_QUEUE` | `perishables-tq` | Worker task queue |
| `TEMPORAL_API_KEY` | — | Set to switch to Temporal Cloud (API-key auth) |
| `INVENTORY_SOURCE` | `stub` | `live` to enable real Bolt Gateway reads |
| `BOLT_BASE_URL` | `https://bolt.jumbotail.com` | Inventory Gateway base URL |
| `BOLT_USER_ID` | — | Caller email for the Gateway |
| `BOLT_ORG_ID` | — | Caller `orgprof` ID for the Gateway |
| `BOLT_AUTH_TOKEN` | — | `Bearer <jwt>` for the Gateway |
| `INVENTORY_SELLTHROUGH_WINDOWS_H` | `47,36,24` | OUTWARDED window ladder (hours, widest first) |

---

## 15. Testing

```bash
PYTHONPATH=. pytest tests/
```

Unit tests cover the decision engine's determinism guarantee:

| Test | What it verifies |
|---|---|
| `test_hold_when_on_track` | `proj ≥ q0` → HOLD, no approval required |
| `test_step_to_ceiling_when_lagging_badly` | `ratio < theta_hold` → jump to ceiling, approval required |
| `test_step_one_rung_when_slightly_short` | `theta_hold ≤ ratio < 1` → single step |
| `test_price_is_monotonic_non_increasing` | Already at R2 + on track → HOLD, never step back |
| `test_rte_auto_clear_to_token_past_gate` | RTE + past gate → AUTO_CLEAR to ₹1, no approval |
| `test_non_rte_token_rung_requires_approval` | Non-RTE at R3 → approval required |

**E2E without Docker:** use `temporalio.testing.WorkflowEnvironment.start_time_skipping()` with a `Worker` running the full workflow + activities and a SQLite `DATABASE_URL`.

---

## 16. Production readiness backlog

See `docs/PROD_READINESS_BACKLOG.md` for the full list. Priority items:

1. **Golden Eye integration** — replace the stub with the real price-write API call; the idempotency constraint `(run_id, rung)` is already in place.
2. **Q0 source** — opening stock is currently synthetic; no trustworthy live source has been identified yet (`leftQty` is a stale 3-year pile; OUTWARDED gives movements, not a batch opening).
3. **Owner notification** — replace `adapters/notify.py` with My J24 push or FCM.
4. **Catalogue integration** — replace the 9-JPIN stub with a real filtered catalogue query on master shelf-life.
5. **mrp live source** — listing price is now live; mrp still falls back to catalogue placeholder.
6. **Retail media** — replace `adapters/retailmedia.py` with the real second-screen publish API.
