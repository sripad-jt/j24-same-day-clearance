"""SKU-master adapter — resolve shelf life (and light metadata) for a JPIN.

Dead stock can be any SKU, well beyond the 9-item leafy-greens catalogue, so we
read `shelf_life_days` (plus category / product_title / mrp when present) from a
SKU-master parquet published to S3 — read the same way as the intraday profile:
prefer `SKU_MASTER_S3_URI` (downloaded + cached by object version), fall back to a
local `SKU_MASTER_PATH` for tests/offline. Never raises: returns None on any miss
so callers apply their own defaults (e.g. the half-shelf-life assumption).

All functions here are called only from `@activity.defn`s, never the workflow.
"""
from __future__ import annotations

import functools
import logging
import os

log = logging.getLogger("sku_master")

# Accepted column aliases (the master's exact schema may vary by publisher).
_JPIN_COLS = ("JPIN", "jpin", "productId", "PRODUCT_ID", "ITEM_NUMBER", "sku_id")
_SHELF_COLS = ("shelf_life_days", "shelfLifeDays", "SHELF_LIFE_DAYS", "shelf_life")
_CAT_COLS = ("category", "CATEGORY", "categoryName")
_TITLE_COLS = ("product_title", "PRODUCT_TITLE", "productName", "name")
_MRP_COLS = ("mrp", "MRP", "maxRetailPrice")


def _resolve_artifact() -> tuple[str | None, str]:
    """Resolve the SKU-master table to a local (path, version). Prefers
    SKU_MASTER_S3_URI (cached by S3 ETag), else local SKU_MASTER_PATH. Never raises."""
    s3_uri = os.getenv("SKU_MASTER_S3_URI", "").strip()
    if s3_uri:
        try:
            from adapters import _s3

            version = _s3.object_version(s3_uri)
            local = _s3.download_to_cache(s3_uri, version)
            return local, version
        except Exception as e:  # noqa: BLE001
            log.warning("sku-master S3 resolve failed (%s): %s", s3_uri, e)
            return None, ""
    p = os.getenv("SKU_MASTER_PATH", "").strip()
    if p and os.path.exists(p):
        return p, str(os.path.getmtime(p))
    return None, ""


def _pick(row_keys, aliases):
    for a in aliases:
        if a in row_keys:
            return a
    return None


@functools.lru_cache(maxsize=4)
def _load_master(path: str, version: str):
    """Load + index the SKU master by JPIN. Cached by (path, version). Returns
    {jpin: {shelf_life_days, category, product_title, mrp}} or None."""
    try:
        import pandas as pd

        df = pd.read_parquet(path) if path.endswith(".parquet") else pd.read_csv(path)
    except Exception as e:  # noqa: BLE001
        log.warning("sku-master load failed (%s): %s", path, e)
        return None

    cols = set(df.columns)
    jc = _pick(cols, _JPIN_COLS)
    sc = _pick(cols, _SHELF_COLS)
    if not jc or not sc:
        log.warning("sku-master missing jpin/shelf-life columns (have %s)", sorted(cols))
        return None
    cat_c = _pick(cols, _CAT_COLS)
    title_c = _pick(cols, _TITLE_COLS)
    mrp_c = _pick(cols, _MRP_COLS)

    idx: dict[str, dict] = {}
    for row in df.itertuples(index=False):
        d = row._asdict()
        jpin = str(d.get(jc) or "").strip()
        if not jpin:
            continue
        try:
            shelf = int(float(d.get(sc)))
        except (TypeError, ValueError):
            continue
        if shelf <= 0:
            continue
        idx[jpin] = {
            "shelf_life_days": shelf,
            "category": str(d.get(cat_c)) if cat_c else "",
            "product_title": str(d.get(title_c)) if title_c else jpin,
            "mrp": float(d.get(mrp_c)) if mrp_c and d.get(mrp_c) is not None else 0.0,
        }
    return idx or None


def resolve_sku(jpin: str) -> dict | None:
    """Return {shelf_life_days, category, product_title, mrp} for a JPIN, or None
    if there is no master artifact or the JPIN is absent. Never raises."""
    path, version = _resolve_artifact()
    if not path:
        return None
    master = _load_master(path, version)
    if not master:
        return None
    return master.get(str(jpin))
