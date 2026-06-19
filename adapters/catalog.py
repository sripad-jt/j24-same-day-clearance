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


# A deterministic catalogue for the demo. Leafy greens are L=1 (must clear today);
# the RTE line exercises the auto-clear-to-₹1 path past the close gate.
_CATALOG: list[CandidateSku] = [
    CandidateSku("JPIN-PALAK-001", "Fresh Palak (Spinach) 250g", "FNV_LEAFY", False, 1, 39.0, 49.0),
    CandidateSku("JPIN-METHI-002", "Fresh Methi (Fenugreek) 200g", "FNV_LEAFY", False, 1, 29.0, 35.0),
    CandidateSku("JPIN-CORIA-003", "Coriander Bunch 100g", "FNV_LEAFY", False, 1, 19.0, 25.0),
    CandidateSku("JPIN-LETTU-004", "Iceberg Lettuce 300g", "FNV_LEAFY", False, 1, 59.0, 75.0),
    CandidateSku("JPIN-SAMOS-101", "Hot Samosa (pack of 4)", "RTE", True, 1, 60.0, 60.0),
    CandidateSku("JPIN-SANDW-102", "Veg Sandwich", "RTE", True, 1, 45.0, 45.0),
]


def discover_candidates(store_id: str, limit: int) -> list[CandidateSku]:
    return _CATALOG[: max(0, limit)]


def get_candidate(jpin: str) -> CandidateSku | None:
    for c in _CATALOG:
        if c.jpin == jpin:
            return c
    return None
