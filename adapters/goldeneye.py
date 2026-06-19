"""Golden Eye price sink (design §13, §17). Source of truth at the till.

Real impl writes a confirmed catalog + price update. This stub just confirms;
idempotency + the durable record live in db.repo.record_price_change (keyed on
run_id + rung), so retries/replays never double-apply.
"""
from __future__ import annotations

import logging

log = logging.getLogger("goldeneye")


def apply_price(store_id: str, jpin: str, rung: str, to_price: float) -> bool:
    """Write the price to Golden Eye and return confirmation. Stub always confirms."""
    log.info("GoldenEye apply: store=%s jpin=%s rung=%s price=%.2f",
             store_id, jpin, rung, to_price)
    return True
