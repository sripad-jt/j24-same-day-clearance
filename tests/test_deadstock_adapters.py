"""Tests for the dead-stock adapters (posgateway client + SKU-master parquet).

No live posgateway / AWS: httpx is monkeypatched and the parquet is a local CSV.
"""
from __future__ import annotations

import textwrap

import pytest

from adapters import deadstock, sku_master


# --------------------------------------------------------------------------- #
# posgateway dead-stock client
# --------------------------------------------------------------------------- #
class _FakeResp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, payload):
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, headers=None, content=None):
        return _FakeResp(self._payload)


async def _run_fetch(monkeypatch, payload):
    monkeypatch.setenv("POSGATEWAY_TOKEN", "test-token")
    monkeypatch.setattr(deadstock.httpx, "AsyncClient", lambda *a, **k: _FakeClient(payload))
    return await deadstock.fetch_dead_stock("BZID-1")


@pytest.mark.asyncio
async def test_fetch_dead_stock_filters_and_sorts(monkeypatch):
    now_ms = 1_700_000_000_000
    payload = {"data": {"productRestockRecommendationResponses": [
        {"productId": "JPIN-A", "isDeadStock": True,
         "lastSoldTimeStamp": now_ms - 5 * 86_400_000, "productRecommendationRank": 2},
        {"productId": "JPIN-B", "isDeadStock": False},          # filtered out
        {"productId": "JPIN-C", "isDeadStock": True,
         "lastSoldTimeStamp": now_ms - 1 * 86_400_000, "productRecommendationRank": 1},
    ]}}
    items = await _run_fetch(monkeypatch, payload)
    assert [i["jpin"] for i in items] == ["JPIN-C", "JPIN-A"]   # sorted by rank
    assert all(i["days_unsold"] >= 0 for i in items)


@pytest.mark.asyncio
async def test_fetch_dead_stock_unconfigured_returns_empty(monkeypatch):
    monkeypatch.delenv("POSGATEWAY_TOKEN", raising=False)
    assert await deadstock.fetch_dead_stock("BZID-1") == []


# --------------------------------------------------------------------------- #
# SKU-master parquet/CSV reader
# --------------------------------------------------------------------------- #
_CSV = textwrap.dedent(
    """\
    JPIN,shelf_life_days,category,product_title,mrp
    JPIN-9,6,dairy,Paneer 200g,90
    JPIN-Z,0,dairy,Bad Row,10
    """
)


def test_sku_master_local_read(tmp_path, monkeypatch):
    sku_master._load_master.cache_clear()
    monkeypatch.delenv("SKU_MASTER_S3_URI", raising=False)
    p = tmp_path / "skum.csv"
    p.write_text(_CSV)
    monkeypatch.setenv("SKU_MASTER_PATH", str(p))
    row = sku_master.resolve_sku("JPIN-9")
    assert row and row["shelf_life_days"] == 6 and row["product_title"] == "Paneer 200g"
    assert sku_master.resolve_sku("JPIN-Z") is None    # shelf_life 0 dropped
    assert sku_master.resolve_sku("JPIN-absent") is None


def test_sku_master_unconfigured_returns_none(tmp_path, monkeypatch):
    sku_master._load_master.cache_clear()
    monkeypatch.delenv("SKU_MASTER_S3_URI", raising=False)
    monkeypatch.delenv("SKU_MASTER_PATH", raising=False)
    assert sku_master.resolve_sku("JPIN-9") is None
