"""Activity: resolve the intraday demand profile for a JPIN at the current hour.

The workflow computes the nominal IST hour/dow/frac deterministically from its own
elapsed clock and passes them in; this activity does the artifact read (I/O) and
returns an `IntradayProfile`. Keeping the read here preserves the determinism
boundary — `decide_v3`/`project_remaining_demand` receive the shares as plain
values and never touch disk or the clock.
"""
from __future__ import annotations

from temporalio import activity

from adapters import profile as profile_adapter
from shared.models import IntradayProfile


@activity.defn
async def resolve_intraday_profile(
    store_id: str,
    jpin: str,
    dow: int,
    hour: int,
    frac: float,
    open_hour: int,
    close_hour: int,
) -> IntradayProfile:
    r = profile_adapter.resolve_shares(
        store_id, jpin, dow, hour, frac, open_hour, close_hour
    )
    return IntradayProfile(
        store_id=store_id,
        jpin=jpin,
        dow=dow,
        hour=hour,
        cum_share_to_now=r["cum_share_to_now"],
        remaining_share=r["remaining_share"],
        source_level=r["source_level"],
        low_confidence=r["low_confidence"],
        generated_at=r.get("generated_at", ""),
    )
