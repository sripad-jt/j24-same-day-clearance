"""Offer copy (design §16). LLM is optional and off the price path — a slow or
absent LLM must never block a markdown, so this always has a deterministic
template fallback. Customer-facing vocabulary: "Fresh Deal / Closing Soon".
"""
from __future__ import annotations


def offer_copy(product: str, pct_off: float, token_free: bool, enable_llm: bool) -> str:
    """Return shelf/second-screen copy. Deterministic template (LLM stub disabled)."""
    if token_free:
        return f"Closing soon — {product} at a token ₹1. Grab it before we shut!"
    if pct_off <= 0:
        return f"Fresh today: {product}"
    return f"Fresh Deal — {product}, {pct_off:g}% off till close"
