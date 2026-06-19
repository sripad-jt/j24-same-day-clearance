# CLAUDE.md — working in this repo

J24 Same-Day Perishables Clearance: a Temporal markdown agent + React control plane.
Stack mirrors the sibling `j24-store-vision` project (Temporal + FastAPI + Postgres +
React/Vite) but has **no CCTV/vision** — sell-through comes from the Inventory Item
Details API, not cameras.

## The one hard rule: determinism boundary

- **Workflows orchestrate; activities do I/O.** Anything non-deterministic
  (network, DB, time-of-day, randomness, secrets) goes in an `@activity.defn`,
  never in `workflows/markdown.py`.
- The price logic is a **pure function** (`pricing/decision_engine.py`): no I/O,
  no clock reads, time only via passed-in values. Keep it that way — it is the
  audit guarantee and is unit-tested.
- In the workflow, use `workflow.now()` (never `datetime.now()`), and `workflow.
  wait_condition(..., timeout=...)` for timers/approval waits.
- Config (ladder/thresholds) is **snapshotted at run start** via `plan_run`, not
  read live per checkpoint.

## Layout

- `shared/models.py` — Pydantic types crossing the Temporal boundary (one source of truth).
- `pricing/` — `ladder.py` (config) + `decision_engine.py` (pure `decide()`).
- `adapters/` — Protocol-style stubs with synthetic data; **swap these for real
  clients to go live** (catalog, inventory, goldeneye, notify, retailmedia, copy_llm).
- `activities/` — thin `@activity.defn` wrappers (pipeline) + DB persistence.
- `workflows/markdown.py` — `PerishableMarkdownWorkflow` (signals, query, ladder loop).
- `db/` — SQLAlchemy read-model + ledger; `init_db()` auto-creates tables (no migrations).
- `api/main.py` — FastAPI control plane (CORS `*`). `web/` — React app.

## Conventions

- Workflow IDs: `perish-markdown-{store}-{jpin}-{receipt_date}`, reuse policy
  `ALLOW_DUPLICATE`, task queue `perishables-tq`.
- Signals: `owner_decision`, `additional_grn`, `sold_out`, `manual_override`.
  Query: `current_state`.
- Activity options live in `_READ/_NOTIFY/_APPLY/_LLM/_DB` presets in the workflow
  (per-activity retry/timeout matrix — design §7). `apply_price_goldeneye` is gated
  on confirmation and idempotent on `(run_id, rung)`.
- `demo_speed` compresses nominal hours into seconds for demos (real time = 1.0).

## Verify

- Unit: `PYTHONPATH=. pytest tests/`.
- E2E without Docker: `temporalio.testing.WorkflowEnvironment.start_time_skipping()`
  + a `Worker` with the workflow/activities + a SQLite `DATABASE_URL`.
- Full stack: `docker compose up --build`.

There is no linter/formatter configured — don't invent one.
