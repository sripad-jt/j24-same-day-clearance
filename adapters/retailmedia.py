"""Retail media + POS second screen (design §13, §17).

Real impl hands the offer (slot payload + QR/CTA) to Vaibhav's AMP platform and
the POS second screen. This stub returns the published payload for the ledger.
"""
from __future__ import annotations

import logging

log = logging.getLogger("retailmedia")


def publish_offer(store_id: str, jpin: str, headline: str, price: float) -> dict:
    log.info("Publish offer: %s %s '%s' @ ₹%.2f", store_id, jpin, headline, price)
    return {
        "store_id": store_id,
        "jpin": jpin,
        "headline": headline,
        "price": price,
        "qr_cta": f"https://j24.deal/{jpin}",
        "channels": ["retail_media", "pos_second_screen"],
    }
