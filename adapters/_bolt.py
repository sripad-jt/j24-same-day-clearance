"""Thin async client for the Inventory Item Details API (Bolt Gateway).

    POST {BOLT_BASE_URL}/api/space/product/details/for-state-status-facility

See docs/inventory-item-details-api.md. Config + secrets come from env (set in
.env): BOLT_BASE_URL, BOLT_USER_ID, BOLT_ORG_ID, BOLT_AUTH_TOKEN. `orgId` is the
*caller's* org (not the store's); the store is selected by `facilityId`.

This is the only place that talks HTTP to the gateway. It is called from
`adapters/inventory.py`, which is itself called only from `@activity.defn`s — so
the determinism boundary is preserved (no network in the workflow).
"""
from __future__ import annotations

import asyncio
import contextvars
import logging
import os

import httpx

log = logging.getLogger("bolt")

# Per-run gateway override (set inside a Bolt-facing activity for a "mock" run).
# A ContextVar is task-local: asyncio copies the context when Temporal starts each
# activity task, so a mock run and a live run executing concurrently never bleed
# into each other. None = use the .env-configured live gateway.
_gw_override: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "bolt_gateway", default=None
)


def use_mock_gateway() -> None:
    """Point this activity's Bolt calls at the local mock gateway (tools/mock_bolt).
    Call at the top of a Bolt-facing activity when the run chose the mock source."""
    _gw_override.set({
        "base_url": os.getenv("MOCK_BOLT_URL", "http://localhost:9099").rstrip("/"),
        "auth_token": os.getenv("MOCK_BOLT_TOKEN", "mock"),
        "user_id": os.getenv("BOLT_USER_ID", "mock"),
        "org_id": os.getenv("BOLT_ORG_ID", "mock"),
    })

_PATH = "/api/space/product/details/for-state-status-facility"
_COUNT_PATH = "/api/space/product/count/for-state-status-facility"

ACTIVE_STATES = ["SELLABLE", "FULFILMENT", "INWARDED", "UNDER_TRANSFER"]
ACTIVE_STATUSES = ["ACTIVE", "ONHOLD"]
OUTWARDED_STATES = ["OUTWARDED"]
OUTWARDED_STATUSES = ["ACTIVE", "EXHAUSTED"]


def configured() -> bool:
    """True when a gateway is usable — either a mock override is set for this run,
    or the live source is selected with creds present."""
    if _gw_override.get() is not None:
        return True
    return (
        os.getenv("INVENTORY_SOURCE", "stub").lower() == "live"
        and bool(os.getenv("BOLT_AUTH_TOKEN"))
    )


def _headers() -> dict:
    o = _gw_override.get()
    if o is not None:
        return {
            "userId": o["user_id"], "orgId": o["org_id"],
            "Authorization": o["auth_token"], "Content-Type": "application/json",
        }
    return {
        "userId": os.getenv("BOLT_USER_ID", ""),
        "orgId": os.getenv("BOLT_ORG_ID", ""),
        "Authorization": os.getenv("BOLT_AUTH_TOKEN", ""),
        "Content-Type": "application/json",
    }


def _is_retryable(exc: Exception) -> bool:
    if isinstance(exc, (httpx.TimeoutException, httpx.NetworkError)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500
    return False


async def _post_with_retry(
    path: str,
    body: dict,
    timeout: float,
    max_attempts: int = 2,
    backoff_s: float = 2.0,
) -> dict:
    """POST with retry on transient failures (timeout / 5xx). Non-retryable errors raise immediately."""
    o = _gw_override.get()
    base = (o["base_url"] if o is not None
            else os.getenv("BOLT_BASE_URL", "https://bolt.jumbotail.com").rstrip("/"))
    last_exc: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            async with httpx.AsyncClient(timeout=timeout) as cx:
                r = await cx.post(base + path, json=body, headers=_headers())
                r.raise_for_status()
                return r.json()
        except Exception as exc:  # noqa: BLE001
            if not _is_retryable(exc):
                raise
            last_exc = exc
            if attempt < max_attempts:
                wait = backoff_s * attempt
                log.warning("bolt %s attempt %d/%d failed (%s) — retry in %.1fs",
                            path, attempt, max_attempts, type(exc).__name__, wait)
                await asyncio.sleep(wait)
    raise last_exc  # type: ignore[misc]


async def details(
    jpins: list[str],
    facility_id: str,
    states: list[str],
    statuses: list[str],
    created_after_ms: int | None = None,
    max_results: int | None = None,
    timeout: float = 100.0,
) -> list[dict]:
    """POST the details query; return the `data[]` array (raises on non-retryable error)."""
    body: dict = {
        "jpins": jpins,
        "facilityId": facility_id,
        "inventoryItemStates": states,
        "inventoryItemStatuses": statuses,
    }
    if created_after_ms is not None:
        body["createdTimeAfter"] = created_after_ms
    if max_results is not None:
        body["maxResults"] = max_results

    return (await _post_with_retry(_PATH, body, timeout)).get("data") or []


async def counts(
    jpins: list[str],
    facility_id: str,
    states: list[str],
    statuses: list[str],
    created_after_ms: int | None = None,
    timeout: float = 100.0,
) -> dict[str, int]:
    """Lightweight per-JPIN quantity counts (`{jpin: qty}`).

    The counterpart to `details()` — same request, but returns just the summed
    quantity per JPIN (a ~100-byte map) instead of every inventory-item row. Far
    faster/smaller, so this is what we use for live on-hand.
    """
    body: dict = {
        "jpins": jpins,
        "facilityId": facility_id,
        "inventoryItemStates": states,
        "inventoryItemStatuses": statuses,
    }
    if created_after_ms is not None:
        body["createdTimeAfter"] = created_after_ms

    return (await _post_with_retry(_COUNT_PATH, body, timeout)).get("data") or {}
