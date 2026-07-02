"""Time-skipping workflow E2E tests (opt-in).

These spin up a real Temporal test server (downloaded + cached on first run) and
exercise the full workflow → activity → SQLite path, faking only the single
live-Bolt activity. They are heavier than the unit suite and need the test-server
binary, so they only run when RUN_E2E=1 is set:

    RUN_E2E=1 PYTHONPATH=. pytest tests/test_workflows_e2e.py -q

Each E2E is a self-contained script under tests/e2e/ that sets its own temp SQLite
DATABASE_URL at import time; we run it in a subprocess so that env is isolated from
the rest of the (Postgres-defaulting) suite. A non-zero exit = assertion failure.
"""
from __future__ import annotations

import os
import subprocess
import sys

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_E2E") not in ("1", "true", "yes"),
    reason="set RUN_E2E=1 to run the time-skipping workflow E2E tests",
)


def _run(script_rel: str) -> None:
    script = os.path.join(_REPO_ROOT, script_rel)
    env = {**os.environ, "PYTHONPATH": _REPO_ROOT}
    proc = subprocess.run(
        [sys.executable, script],
        cwd=_REPO_ROOT, env=env, capture_output=True, text=True, timeout=600,
    )
    if proc.returncode != 0:
        pytest.fail(
            f"{script_rel} failed (exit {proc.returncode})\n"
            f"--- stdout ---\n{proc.stdout[-4000:]}\n"
            f"--- stderr ---\n{proc.stderr[-4000:]}"
        )
    assert "PASS:" in proc.stdout, proc.stdout[-2000:]


def test_sim_clearance_e2e():
    _run("tests/e2e/sim_clearance_e2e.py")


def test_deadstock_clearance_e2e():
    _run("tests/e2e/deadstock_clearance_e2e.py")
