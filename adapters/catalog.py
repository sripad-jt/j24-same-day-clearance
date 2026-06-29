"""Stage A — discover the day's perishable candidates (design §4).

Real impl would filter catalog/master by perishable category + master shelf-life.
This stub returns a small synthetic set of leafy-green (L=1) and RTE lines so the
demo has something to clear.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CandidateSku:
    jpin: str
    product_title: str
    category: str
    is_rte: bool
    shelf_life_days: int
    list_price: float
    mrp: float


# Pilot leafy-green catalogue — the real J24 JPINs carried across the Essentials
# stores (product-level, so the same set is offered for any selected store; the
# store only changes the facility for downstream inventory/sell-through reads).
# All are leafy greens, master shelf-life L=1 (must clear today).
#
# NOTE: list_price / mrp here are FALLBACK PLACEHOLDERS. When INVENTORY_SOURCE=live,
# plan_run replaces list_price with the real per-JPIN `listingSellingPrice` from the
# Inventory Item Details API (see adapters/inventory.live_listing_price); these values
# are only used when the live read is disabled or times out. mrp has no live source yet.
_CATALOG: list[CandidateSku] = [
    CandidateSku("JPIN-1304597126", "Coriander Leaves Bunch", "FNV_LEAFY", False, 1, 15.0, 20.0),
    CandidateSku("JPIN-1304597236", "Curry Leaves", "FNV_LEAFY", False, 1, 12.0, 15.0),
    CandidateSku("JPIN-1304597122", "Mint / Pudina Leaves", "FNV_LEAFY", False, 1, 15.0, 20.0),
    CandidateSku("JPIN-1304597163", "Spinach Leaves", "FNV_LEAFY", False, 1, 25.0, 30.0),
    CandidateSku("JPIN-1304597127", "Methi Leaves", "FNV_LEAFY", False, 1, 29.0, 35.0),
    CandidateSku("JPIN-1304521194", "Dill Leaves", "FNV_LEAFY", False, 1, 20.0, 25.0),
    CandidateSku("JPIN-1304562941", "Amaranthus Red Bunch", "FNV_LEAFY", False, 1, 18.0, 22.0),
    CandidateSku("JPIN-1304565447", "Amaranthus Green Bunch", "FNV_LEAFY", False, 1, 18.0, 22.0),
    CandidateSku("JPIN-1304193294", "Neem Leaves 1 Bunch, 1Pc", "FNV_LEAFY", False, 1, 10.0, 12.0),
]


def discover_candidates(store_id: str, limit: int) -> list[CandidateSku]:
    return _CATALOG[: max(0, limit)]


def get_candidate(jpin: str) -> CandidateSku | None:
    for c in _CATALOG:
        if c.jpin == jpin:
            return c
    return None
