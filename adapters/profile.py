"""Intraday-profile adapter — resolve hour-of-day demand shares for a JPIN.

Reads the artifact produced by the sibling hourly pipeline
(`pipelines/hourly`): one row per (STORE_ID, ITEM_NUMBER, dow, hour) with a
`share` column that sums to 1.0 across the 24 hours of a (store, item, dow) cell.
We collapse that 24-vector to the two scalars the projector needs at the current
moment — `cum_share_to_now` and `remaining_share` — plus provenance.

Design notes
------------
* Only the *shape* (share curve) is used, never the absolute `hourly_units`. The
  same-day sell-through supplies the level; the profile supplies the shape. This
  keeps v3 robust to a stale or missing daily point-forecast.
* The artifact is a small Parquet/CSV produced by a SEPARATE hourly-forecast
  workflow (not in this repo) and published to S3. We only *read* it: prefer
  `INTRADAY_PROFILE_S3_URI` (downloaded + cached by object version), and fall
  back to a local `INTRADAY_PROFILE_PATH` for tests/offline. Resolution is cheap
  and cached; a re-published artifact busts the cache via its S3 ETag.
* On any miss (no artifact, JPIN absent, sparse cell) we fall back to a synthetic
  evening-peaked leafy-greens curve so behaviour is still peak-aware in demos and
  the pilot's cold-start — flagged `source_level="synthetic"`, `low_confidence`.

All functions here are called only from `@activity.defn`s, never the workflow.
"""
from __future__ import annotations

import functools
import logging
import os
from bisect import bisect_right

log = logging.getLogger("profile")

# Synthetic fallback: relative hourly demand weights for leafy greens, 05:00-21:00
# IST. Morning restock bump (~7-9), midday trough, strong evening peak (18-20).
# Values are relative; normalised to a share vector on use.
_SYNTH_WEIGHTS = {
    5: 0.5, 6: 1.0, 7: 2.0, 8: 2.5, 9: 2.2, 10: 1.6, 11: 1.3, 12: 1.4,
    13: 1.3, 14: 1.1, 15: 1.2, 16: 1.8, 17: 2.8, 18: 4.2, 19: 4.5, 20: 3.0,
    21: 1.2,
}


def _ist_today() -> str:
    """Today's date (YYYY-MM-DD) in IST — used to template dated S3 keys. This runs
    activity-side (I/O), never in the workflow, so a wall-clock read is fine."""
    from datetime import datetime, timedelta, timezone

    ist = timezone(timedelta(hours=5, minutes=30))
    return datetime.now(ist).strftime("%Y-%m-%d")


def _resolve_artifact(store_id: str) -> tuple[str | None, str]:
    """Resolve the profile table to a local (path, version).

    Prefers `INTRADAY_PROFILE_S3_URI` (may contain `{store}`/`{date}` placeholders),
    downloading + caching by the object's S3 version tag so a re-publish busts the
    cache. Falls back to a local `INTRADAY_PROFILE_PATH` (version = mtime) for
    tests/offline. Returns `(None, "")` on any miss — the caller then goes
    synthetic. Never raises.
    """
    s3_uri = os.getenv("INTRADAY_PROFILE_S3_URI", "").strip()
    if s3_uri:
        try:
            from adapters import _s3

            uri = s3_uri.format(store=store_id, date=_ist_today())
            version = _s3.object_version(uri)
            local = _s3.download_to_cache(uri, version)
            return local, version
        except Exception as e:  # noqa: BLE001
            log.warning("profile S3 resolve failed (%s): %s", s3_uri, e)
            return None, ""

    p = os.getenv("INTRADAY_PROFILE_PATH", "").strip()
    if p and os.path.exists(p):
        return p, str(os.path.getmtime(p))
    return None, ""


@functools.lru_cache(maxsize=8)
def _load_profile(path: str, version: str):
    """Load + index the profile table. Cached by (path, version) so a re-published
    artifact busts the cache. Returns {(store, jpin, dow): {hour: share}} or None.
    """
    try:
        import pandas as pd

        df = pd.read_parquet(path) if path.endswith(".parquet") else pd.read_csv(path)
    except Exception as e:  # noqa: BLE001
        log.warning("profile load failed (%s): %s", path, e)
        return None

    need = {"STORE_ID", "ITEM_NUMBER", "dow", "hour", "share"}
    if not need.issubset(df.columns):
        log.warning("profile missing columns %s", need - set(df.columns))
        return None

    idx: dict[tuple[str, str, int], dict[int, float]] = {}
    src: dict[tuple[str, str, int], str] = {}
    for row in df.itertuples(index=False):
        key = (str(row.STORE_ID), str(row.ITEM_NUMBER), int(row.dow))
        idx.setdefault(key, {})[int(row.hour)] = float(row.share)
        if hasattr(row, "source_level"):
            src[key] = str(row.source_level)
    return {"idx": idx, "src": src, "generated_at": _gen_at(df)}


def _gen_at(df) -> str:
    try:
        if "generated_at" in df.columns and len(df):
            return str(df["generated_at"].iloc[0])
    except Exception:  # noqa: BLE001
        pass
    return ""


def _shares_to_cum(hour_shares: dict[int, float], hour: int, frac: float,
                   open_hour: int, close_hour: int) -> tuple[float, float]:
    """Collapse an hour->share map to (cum_share_to_now, remaining_share).

    `frac` is the fraction elapsed within the current hour (0..1). Demand outside
    [open, close] is treated as zero and the vector is renormalised over open
    hours so the two scalars always sum to ~1.0 within the selling day.
    """
    total = sum(s for h, s in hour_shares.items() if open_hour <= h <= close_hour)
    if total <= 0:
        return 0.0, 0.0
    cum = 0.0
    for h in range(open_hour, close_hour + 1):
        s = hour_shares.get(h, 0.0) / total
        if h < hour:
            cum += s
        elif h == hour:
            cum += s * max(0.0, min(1.0, frac))
    cum = max(0.0, min(1.0, cum))
    return cum, max(0.0, 1.0 - cum)


def _synthetic(hour: int, frac: float, open_hour: int, close_hour: int) -> tuple[float, float]:
    weights = {h: w for h, w in _SYNTH_WEIGHTS.items() if open_hour <= h <= close_hour}
    return _shares_to_cum(weights, hour, frac, open_hour, close_hour)


def resolve_shares(
    store_id: str,
    jpin: str,
    dow: int,
    hour: int,
    frac: float = 0.0,
    open_hour: int = 8,
    close_hour: int = 21,
) -> dict:
    """Return {cum_share_to_now, remaining_share, source_level, low_confidence,
    generated_at} for one JPIN at the current moment. Never raises: on any miss
    it returns the synthetic evening-peaked curve.
    """
    path, version = _resolve_artifact(store_id)
    if path:
        prof = _load_profile(path, version)
        if prof:
            key = (store_id, jpin, dow)
            hour_shares = prof["idx"].get(key)
            if hour_shares:
                cum, rem = _shares_to_cum(hour_shares, hour, frac, open_hour, close_hour)
                if cum > 0 or rem > 0:
                    return {
                        "cum_share_to_now": cum,
                        "remaining_share": rem,
                        "source_level": prof["src"].get(key, "sku"),
                        "low_confidence": False,
                        "generated_at": prof.get("generated_at", ""),
                    }
    cum, rem = _synthetic(hour, frac, open_hour, close_hour)
    return {
        "cum_share_to_now": cum,
        "remaining_share": rem,
        "source_level": "synthetic",
        "low_confidence": True,
        "generated_at": "",
    }
