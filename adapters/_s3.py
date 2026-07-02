"""Minimal S3 read helpers for consuming the external forecast artifact.

The intraday demand-`share` curve is produced by a SEPARATE hourly-forecast
workflow (not in this repo) that publishes its output to S3. Here we only need to
*read* the latest published artifact: parse an ``s3://`` URI, get the object's
current version tag (ETag) so a re-publish busts our cache, and download it to a
local cache path.

``boto3`` is imported lazily inside the functions so this package imports fine
without it — callers (``adapters/profile.py``) fall back to a synthetic curve if
S3/boto3 is unavailable. Never used from a workflow, only from ``@activity.defn``.
"""
from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path
from urllib.parse import urlparse

log = logging.getLogger("s3")

_DEFAULT_REGION = (
    os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or "ap-south-1"
)


def parse_s3_uri(uri: str) -> tuple[str, str]:
    """Split ``s3://bucket/key`` into ``(bucket, key)``."""
    parsed = urlparse(uri)
    if parsed.scheme != "s3":
        raise ValueError(f"Expected s3:// URI, got {uri!r}")
    return parsed.netloc, parsed.path.lstrip("/")


def _client():
    import boto3  # lazy: optional dependency

    return boto3.client("s3", region_name=_DEFAULT_REGION)


def object_version(uri: str) -> str:
    """Return the current version tag of an S3 object (ETag, else LastModified).

    Used as a cache key so a re-published artifact busts the local cache. Raises
    on any S3 error — the caller is expected to treat that as a miss.
    """
    bucket, key = parse_s3_uri(uri)
    head = _client().head_object(Bucket=bucket, Key=key)
    etag = str(head.get("ETag", "")).strip('"')
    if etag:
        return etag
    lm = head.get("LastModified")
    return str(lm) if lm else ""


def download_to_cache(uri: str, version: str, cache_dir: str | None = None) -> str:
    """Download ``s3://...`` to a local cache path keyed by ``(uri, version)``.

    Returns the local path. Skips the download when the versioned file already
    exists, so repeated reads of an unchanged artifact hit disk, not the network.
    """
    bucket, key = parse_s3_uri(uri)
    base = cache_dir or os.getenv("INTRADAY_PROFILE_CACHE_DIR") or os.path.join(
        tempfile.gettempdir(), "intraday-profile-cache"
    )
    Path(base).mkdir(parents=True, exist_ok=True)
    suffix = os.path.splitext(key)[1] or ".bin"
    safe_ver = (version or "novers").replace("/", "_").replace(":", "_")
    dest = os.path.join(base, f"{safe_ver}{suffix}")
    if not os.path.exists(dest):
        _client().download_file(bucket, key, dest)
        log.info("profile artifact downloaded %s -> %s", uri, dest)
    return dest
