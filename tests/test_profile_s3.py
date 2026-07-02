"""Tests for the intraday-profile adapter's artifact resolution.

Covers the three read paths of `adapters/profile.py`:
  (a) nothing configured -> synthetic evening-peaked fallback (never raises);
  (b) a local `INTRADAY_PROFILE_PATH` fixture -> real shares;
  (c) an S3 URI -> boto3 head/download (monkeypatched), cached by object version.

No live AWS, no boto3 install needed — the S3 client is fully monkeypatched.
"""
from __future__ import annotations

import textwrap

import adapters.profile as profile
from adapters import _s3


_CSV = textwrap.dedent(
    """\
    STORE_ID,ITEM_NUMBER,dow,hour,share,source_level,generated_at
    BZID-1,JPIN-1,2,8,0.1,sku,2026-07-01
    BZID-1,JPIN-1,2,12,0.2,sku,2026-07-01
    BZID-1,JPIN-1,2,16,0.3,sku,2026-07-01
    BZID-1,JPIN-1,2,19,0.4,sku,2026-07-01
    """
)


def _clear_env(monkeypatch):
    monkeypatch.delenv("INTRADAY_PROFILE_S3_URI", raising=False)
    monkeypatch.delenv("INTRADAY_PROFILE_PATH", raising=False)
    profile._load_profile.cache_clear()


def test_no_artifact_falls_back_to_synthetic(monkeypatch):
    _clear_env(monkeypatch)
    r = profile.resolve_shares("BZID-1", "JPIN-1", dow=2, hour=16, frac=0.5)
    assert r["source_level"] == "synthetic"
    assert r["low_confidence"] is True
    assert abs(r["cum_share_to_now"] + r["remaining_share"] - 1.0) < 1e-6


def test_local_path_reads_real_shares(tmp_path, monkeypatch):
    _clear_env(monkeypatch)
    p = tmp_path / "prof.csv"
    p.write_text(_CSV)
    monkeypatch.setenv("INTRADAY_PROFILE_PATH", str(p))

    r = profile.resolve_shares("BZID-1", "JPIN-1", dow=2, hour=16, frac=0.0)
    # shares before hour 16 = 0.1 + 0.2 = 0.3 of the total 1.0
    assert r["source_level"] == "sku"
    assert abs(r["cum_share_to_now"] - 0.3) < 1e-6
    assert abs(r["remaining_share"] - 0.7) < 1e-6


class _FakeS3Client:
    """Minimal fake: serves _CSV, versioned by a mutable ETag; counts downloads."""

    def __init__(self, csv_text, etag):
        self.csv_text = csv_text
        self.etag = etag
        self.downloads = 0
        self.heads = 0

    def head_object(self, Bucket, Key):  # noqa: N803 (boto3 kwarg names)
        self.heads += 1
        return {"ETag": f'"{self.etag}"'}

    def download_file(self, Bucket, Key, dest):  # noqa: N803
        self.downloads += 1
        with open(dest, "w") as f:
            f.write(self.csv_text)


def test_s3_uri_downloads_and_caches_by_version(tmp_path, monkeypatch):
    _clear_env(monkeypatch)
    fake = _FakeS3Client(_CSV, etag="v1")
    monkeypatch.setattr(_s3, "_client", lambda: fake)
    monkeypatch.setenv("INTRADAY_PROFILE_CACHE_DIR", str(tmp_path / "cache"))
    # templating: {store}/{date} must resolve without error
    monkeypatch.setenv(
        "INTRADAY_PROFILE_S3_URI",
        "s3://bucket/prefix/{store}/{date}/intraday_shares.csv",
    )

    r1 = profile.resolve_shares("BZID-1", "JPIN-1", dow=2, hour=16, frac=0.0)
    assert r1["source_level"] == "sku"
    assert abs(r1["cum_share_to_now"] - 0.3) < 1e-6
    assert fake.downloads == 1

    # Second read, same version -> served from local cache, no new download.
    profile._load_profile.cache_clear()
    r2 = profile.resolve_shares("BZID-1", "JPIN-1", dow=2, hour=16, frac=0.0)
    assert r2["source_level"] == "sku"
    assert fake.downloads == 1  # unchanged: ETag busted nothing

    # Re-published artifact (new ETag) -> cache busts, one more download.
    fake.etag = "v2"
    profile._load_profile.cache_clear()
    r3 = profile.resolve_shares("BZID-1", "JPIN-1", dow=2, hour=16, frac=0.0)
    assert r3["source_level"] == "sku"
    assert fake.downloads == 2


def test_s3_error_falls_back_to_synthetic(tmp_path, monkeypatch):
    _clear_env(monkeypatch)

    class _Boom:
        def head_object(self, **_):
            raise RuntimeError("no such bucket")

    monkeypatch.setattr(_s3, "_client", lambda: _Boom())
    monkeypatch.setenv("INTRADAY_PROFILE_S3_URI", "s3://bucket/missing.csv")

    r = profile.resolve_shares("BZID-1", "JPIN-1", dow=2, hour=16, frac=0.5)
    assert r["source_level"] == "synthetic"
    assert r["low_confidence"] is True
