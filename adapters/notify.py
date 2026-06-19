"""My J24 owner surface (design §8, §17). Approval cards + post-hoc notifications.

Real impl pushes to notification.prod.jumbotail.com. This stub logs; the card
itself is represented as a run event + the awaiting_approval flag the React app
reads, and the owner responds via the API (ownerDecision signal).
"""
from __future__ import annotations

import logging

log = logging.getLogger("notify")


def push_approval_card(store_id: str, jpin: str, product: str,
                       from_price: float, to_price: float,
                       units_left: int, reason: str) -> str:
    log.info("My J24 card: %s %s ₹%.2f→₹%.2f (%d left) — %s",
             store_id, product, from_price, to_price, units_left, reason)
    return (
        f"{product}: ₹{from_price:g} → ₹{to_price:g} · {units_left} left · {reason}"
    )


def notify_owner(store_id: str, message: str) -> None:
    log.info("My J24 notify: %s — %s", store_id, message)
