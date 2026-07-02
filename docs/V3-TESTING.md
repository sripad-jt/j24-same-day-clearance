# v3 — how to test

Three layers, fastest first. All commands from the repo root.

## 1. Unit tests (pure logic — no services)

```bash
uv pip install -e .            # or: pip install temporalio fastapi sqlalchemy "psycopg[binary]" pydantic pandas numpy pytest httpx
PYTHONPATH=. pytest tests/ -q
```

`tests/test_projection.py` is the v3 proof: the evening-peak case where v2 marks
down and v3 HOLDs, the laggard that still steps, early-day rate-leaning, and
determinism. `tests/test_decision_engine.py` guards the v2 policy is unchanged.

## 2. Full loop against the stateful mock (the ladder actually walks)

No real Bolt creds needed. Four terminals:

```bash
# 0. config
cp .env.example .env

# 1. mock gateway — evening-peaked, time-moving sell-through
uvicorn tools.mock_bolt:app --port 9099

# 2. bring up Postgres + Temporal + worker + api + web
docker compose up --build
#    (or locally: temporal server start-dev ; then python worker.py)

# 3. point the app at the mock and start a run (compressed 13h day ~30s)
export INVENTORY_SOURCE=live BOLT_BASE_URL=http://localhost:9099 BOLT_AUTH_TOKEN=mock
export PROJECTION_MODE=v3 MOCK_SPEED=1800
python starter.py --store BZID-1304298141 --jpin JPIN-1304597126 --speed 1800
```

Watch the run in the web app (http://localhost:8080) or Temporal UI
(http://localhost:8081): sell-through rises on the mock's curve, the projection
updates each tick, and the price steps only when the profile-aware projection
says the line won't clear. Flip `PROJECTION_MODE=v2` and re-run the same JPIN to
see the flat model mark down earlier through the afternoon trough.

## 3. Shared snapshot poller (optional — the read-model path)

```bash
export READ_FROM_SNAPSHOT=true
python start_poller.py --store BZID-1304298141 --speed 1800   # one per facility
python starter.py --store BZID-1304298141 --jpin JPIN-1304597126 --speed 1800
```

Now batches read the shared `sell_through_snapshot` (one batched OUTWARDED scan
per facility per tick) instead of each scanning Bolt. With the poller down, the
batch falls back to a direct read after the snapshot goes stale.

## Real intraday profile (optional)

By default `adapters/profile.py` uses a synthetic evening-peaked curve, so v3
runs with no artifact. The real artifact is produced by a **separate** hourly-
forecast workflow (not in this repo) that publishes to S3; point at it with
`INTRADAY_PROFILE_S3_URI=s3://bucket/prefix/{store}/{date}/intraday_shares.parquet`
(cached locally, busted by ETag). For offline tests use the local override
`INTRADAY_PROFILE_PATH=/path/to/profile.parquet`. Either way the columns are:
`STORE_ID, ITEM_NUMBER, dow, hour, share, source_level`.
