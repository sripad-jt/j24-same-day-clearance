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
