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

import os

import httpx

_PATH = "/api/space/product/details/for-state-status-facility"
_COUNT_PATH = "/api/space/product/count/for-state-status-facility"

ACTIVE_STATES = ["SELLABLE", "FULFILMENT", "INWARDED", "UNDER_TRANSFER"]
ACTIVE_STATUSES = ["ACTIVE", "ONHOLD"]
OUTWARDED_STATES = ["OUTWARDED"]
OUTWARDED_STATUSES = ["ACTIVE", "EXHAUSTED"]


def configured() -> bool:
    """True when the live source is selected and creds are present."""
    return (
        os.getenv("INVENTORY_SOURCE", "stub").lower() == "live"
        and bool(os.getenv("BOLT_AUTH_TOKEN"))
    )


def _headers() -> dict:
    return {
        "userId": os.getenv("BOLT_USER_ID", ""),
        "orgId": os.getenv("BOLT_ORG_ID", ""),
        "Authorization": os.getenv("BOLT_AUTH_TOKEN", ""),
        "Content-Type": "application/json",
    }


async def details(
    jpins: list[str],
    facility_id: str,
    states: list[str],
    statuses: list[str],
    created_after_ms: int | None = None,
    max_results: int | None = None,
    timeout: float = 45.0,
) -> list[dict]:
    """POST the details query; return the `data[]` array (raises on HTTP error)."""
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

    base = os.getenv("BOLT_BASE_URL", "https://bolt.jumbotail.com").rstrip("/")
    async with httpx.AsyncClient(timeout=timeout) as cx:
        r = await cx.post(base + _PATH, json=body, headers=_headers())
        r.raise_for_status()
        return r.json().get("data") or []


async def counts(
    jpins: list[str],
    facility_id: str,
    states: list[str],
    statuses: list[str],
    created_after_ms: int | None = None,
    timeout: float = 50.0,
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

    base = os.getenv("BOLT_BASE_URL", "https://bolt.jumbotail.com").rstrip("/")
    async with httpx.AsyncClient(timeout=timeout) as cx:
        r = await cx.post(base + _COUNT_PATH, json=body, headers=_headers())
        r.raise_for_status()
        return r.json().get("data") or {}
