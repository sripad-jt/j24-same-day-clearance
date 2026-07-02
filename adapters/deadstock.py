"""Dead-stock source — posgateway recommendation API (read-only).

Dead/slow-moving stock is detected upstream by posgateway, the same source the
sibling `j24-pulse` project uses. We only read it:

    POST {POSGATEWAY_BASE_URL}/api/recommendation/dead-stock/{store_id}
    Headers: Authorization: Bearer <POSGATEWAY_TOKEN>, appversion: 1
    Body:    empty

The endpoint is slow (minutes for some stores) and load-balances across instances
that don't all have data loaded, so 404s appear randomly for valid stores — we use
a long read timeout (DEADSTOCK_TIMEOUT_S, default 180s) and retry on 404/5xx/network,
bailing immediately on 401/403. Best-effort: returns [] on failure so a discovery
run degrades to "nothing flagged" rather than crashing.

Called only from `@activity.defn`s, never the workflow.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time

import httpx

log = logging.getLogger("deadstock")

_DEAD_STOCK_PATH = "/api/recommendation/dead-stock/{store_id}"
_MAX_ATTEMPTS = 3
_RETRY_DELAY_S = 5.0

# Envelope keys posgateway has used to wrap the recommendation array.
_WRAPPERS = (
    "productRestockRecommendationResponses", "data", "result", "response",
    "recommendations", "products", "items", "deadStockItems", "dead_stock",
)


def _base_url() -> str:
    return os.getenv("POSGATEWAY_BASE_URL", "http://posgateway.prod.jumbotail.com").rstrip("/")


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {os.getenv('POSGATEWAY_TOKEN', '')}",
        "appversion": "1",
    }


def _timeout_s() -> float:
    return float(os.getenv("DEADSTOCK_TIMEOUT_S", "180"))


def configured() -> bool:
    return bool(os.getenv("POSGATEWAY_TOKEN"))


def _extract_items(payload) -> list[dict]:
    """Pull the dead-stock array out of the response regardless of envelope key."""
    if isinstance(payload, list):
        return [it for it in payload if isinstance(it, dict)]
    if isinstance(payload, dict):
        for w in _WRAPPERS:
            v = payload.get(w)
            if isinstance(v, list):
                return [it for it in v if isinstance(it, dict)]
            if isinstance(v, dict):
                inner = _extract_items(v)
                if inner:
                    return inner
    return []


async def fetch_dead_stock(store_id: str) -> list[dict]:
    """Return dead-stock items for a store, most-urgent first.

    Each item: {"jpin": str, "days_unsold": int, "last_sold_ms": int, "rank": int}.
    Returns [] on any error or when nothing is flagged isDeadStock.
    """
    if not configured():
        log.warning("posgateway not configured (POSGATEWAY_TOKEN unset) — no dead stock")
        return []

    url = f"{_base_url()}{_DEAD_STOCK_PATH.format(store_id=store_id)}"
    timeout = httpx.Timeout(_timeout_s())
    payload = None
    last_err: str | None = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            async with httpx.AsyncClient(timeout=timeout) as cx:
                resp = await cx.post(url, headers=_headers(), content="")
            if resp.status_code == 200:
                payload = resp.json()
                break
            if resp.status_code in (401, 403):
                last_err = f"auth_error_{resp.status_code}"
                break
            last_err = f"http_{resp.status_code}"
        except Exception as e:  # noqa: BLE001 - best-effort live read
            last_err = f"{type(e).__name__}: {e}"
        if attempt < _MAX_ATTEMPTS:
            await asyncio.sleep(_RETRY_DELAY_S)

    if payload is None:
        log.warning("posgateway dead-stock failed store=%s attempts=%d last=%s",
                    store_id, _MAX_ATTEMPTS, last_err)
        return []

    now_ms = int(time.time() * 1000)
    out: list[dict] = []
    for it in _extract_items(payload):
        if not it.get("isDeadStock"):
            continue
        jpin = it.get("productId") or ""
        if not jpin:
            continue
        try:
            last_sold_ms = int(it.get("lastSoldTimeStamp") or 0)
        except (TypeError, ValueError):
            last_sold_ms = 0
        days_unsold = (now_ms - last_sold_ms) // 86_400_000 if last_sold_ms else 0
        out.append({
            "jpin": jpin,
            "days_unsold": max(int(days_unsold), 0),
            "last_sold_ms": last_sold_ms,
            "rank": int(it.get("productRecommendationRank") or 1_000_000),
        })
    out.sort(key=lambda x: x["rank"])
    log.info("posgateway dead-stock store=%s items=%d", store_id, len(out))
    return out
