"""Load `.env` into the process environment for local runs.

Imported for its side effect by `db.database` and `shared.config` (the two modules
that read `os.getenv` at import time), so entrypoints — worker, API, CLIs — pick up
`.env` automatically. This avoids `source .env`, which chokes on values containing
shell metacharacters (e.g. JWT tokens on their own wrapped line).

No-op if python-dotenv isn't installed or there's no `.env`. Uses override=False so
real environment variables (e.g. a test's DATABASE_URL) always win over the file.
"""
from __future__ import annotations


def _load() -> None:
    try:
        from dotenv import load_dotenv
    except Exception:  # noqa: BLE001 - optional dependency
        return
    try:
        load_dotenv(override=False)
    except Exception:  # noqa: BLE001 - never block startup on a malformed .env
        pass


_load()
