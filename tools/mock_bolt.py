"""Stateful mock of the Bolt gateway — trajectory testing for the markdown loop.

A Postman static mock is great for *contract* testing (headers, retry on 5xx,
JSON shape) but its responses don't move, so the ladder never walks. This mock
synthesizes a realistic, *time-moving* OUTWARDED curve: units sold since a
`createdTimeAfter` timestamp follow an evening-peaked leafy-greens profile, so as
wall-clock advances the sell-through rises, the projection updates, and the agent
actually steps the price. Point the app at it with:

    BOLT_BASE_URL=http://localhost:9099  INVENTORY_SOURCE=live  BOLT_AUTH_TOKEN=mock

Run:  uvicorn tools.mock_bolt:app --port 9099

It implements the two endpoints the client calls:
  POST /api/space/product/details/for-state-status-facility   -> per-row details
  POST /api/space/product/count/for-state-status-facility     -> {jpin: qty}

Per-JPIN opening stock and a demand-strength multiplier are derived from a hash
so behaviour is deterministic and varied: some lines clear on their own (HOLD),
others lag (STEP). A `?speed=` multiplier compresses the day for fast demos.
"""
from __future__ import annotations

import hashlib
import os
import time

from fastapi import FastAPI, Request

app = FastAPI(title="mock-bolt")

# 05:00-21:00 IST relative demand weights (evening-peaked); cumulative fraction
# by hour is what drives synthesized sales.
_WEIGHTS = {
    5: 0.5, 6: 1.0, 7: 2.0, 8: 2.5, 9: 2.2, 10: 1.6, 11: 1.3, 12: 1.4,
    13: 1.3, 14: 1.1, 15: 1.2, 16: 1.8, 17: 2.8, 18: 4.2, 19: 4.5, 20: 3.0, 21: 1.2,
}
_OPEN, _CLOSE = 5, 21
_DAY_START_HOUR = 5

# Optional runtime knobs
_SPEED = float(os.getenv("MOCK_SPEED", "1.0"))          # compress the day
_T0_MS = int(os.getenv("MOCK_T0_MS", "0")) or None       # pin T0 for tests


def _q0(jpin: str) -> int:
    h = int(hashlib.sha256((jpin + ":q0").encode()).hexdigest(), 16)
    return 30 + h % 50                                   # 30..79 opening units


def _strength(jpin: str) -> float:
    h = int(hashlib.sha256((jpin + ":s").encode()).hexdigest(), 16)
    return 0.55 + (h % 90) / 100.0                       # 0.55..1.44 of Q0 clears/day


def _cum_frac(hour_f: float) -> float:
    """Cumulative demand fraction elapsed by fractional hour `hour_f`."""
    total = sum(_WEIGHTS.values())
    acc = 0.0
    for h in range(_OPEN, _CLOSE + 1):
        w = _WEIGHTS.get(h, 0.0) / total
        if h + 1 <= hour_f:
            acc += w
        elif h <= hour_f < h + 1:
            acc += w * (hour_f - h)
    return max(0.0, min(1.0, acc))


def _now_hour_f() -> float:
    """Current IST fractional hour, honouring MOCK_SPEED compression from T0."""
    now = time.time()
    if _T0_MS:
        elapsed_h = (now - _T0_MS / 1000.0) / 3600.0 * _SPEED
        return _DAY_START_HOUR + max(0.0, elapsed_h)
    # real wall clock in IST (UTC+5:30)
    ist = time.gmtime(now + 5.5 * 3600)
    return ist.tm_hour + ist.tm_min / 60.0


def _sold_since(jpin: str, since_hour_f: float) -> int:
    day_total = _q0(jpin) * _strength(jpin)
    now_f = _now_hour_f()
    sold_now = day_total * _cum_frac(now_f)
    sold_since_start = day_total * _cum_frac(since_hour_f)
    return int(max(0, round(sold_now - sold_since_start)))


def _hour_f_from_ms(ms: int | None) -> float:
    if not ms:
        return _DAY_START_HOUR
    ist = time.gmtime(ms / 1000.0 + 5.5 * 3600)
    return ist.tm_hour + ist.tm_min / 60.0


@app.post("/api/space/product/count/for-state-status-facility")
async def counts(req: Request):
    body = await req.json()
    jpins = body.get("jpins", [])
    states = set(body.get("inventoryItemStates", []))
    since = _hour_f_from_ms(body.get("createdTimeAfter"))
    data: dict[str, int] = {}
    for j in jpins:
        if "OUTWARDED" in states:
            data[j] = _sold_since(j, since)                 # sales in the window
        else:
            sold_today = _sold_since(j, _DAY_START_HOUR)
            data[j] = max(0, _q0(j) - sold_today)           # on-hand
    return {"data": data}


@app.post("/api/space/product/details/for-state-status-facility")
async def details(req: Request):
    body = await req.json()
    jpins = body.get("jpins", [])
    states = set(body.get("inventoryItemStates", []))
    now = int(time.time() * 1000)
    rows = []
    for j in jpins:
        if "OUTWARDED" in states:
            n = _sold_since(j, _hour_f_from_ms(body.get("createdTimeAfter")))
            for _ in range(n):
                rows.append({
                    "inventoryItem": {"jpin": j, "initialQty": 1, "leftQty": 0},
                    "inventoryItemCreatedTime": now, "listingSellingPrice": 20.0,
                })
        else:
            sold = _sold_since(j, _DAY_START_HOUR)
            rows.append({
                "inventoryItem": {"jpin": j, "initialQty": _q0(j),
                                  "leftQty": max(0, _q0(j) - sold)},
                "inventoryItemCreatedTime": now, "listingSellingPrice": 20.0,
            })
    return {"data": rows}


@app.get("/healthz")
async def healthz():
    return {"ok": True, "hour_ist": round(_now_hour_f(), 2), "speed": _SPEED}
