"""Default markdown ladder + config (design §6). Lives in config, not in code —
ops can re-tune rungs/hours/floor from pilot data without a deploy.
"""
from __future__ import annotations

from shared.models import MarkdownConfig, RungDef

# R0 observe → R1 25% → R2 50% → R3 token ₹1. Checkpoints fire on whichever of
# the elapsed-hour or the IST wall-clock trigger comes first.
DEFAULT_RUNGS: list[RungDef] = [
    RungDef(index=0, label="R0", elapsed_hours=0.0, wallclock_hour_ist=None,
            ceiling_pct=0.0, token_free=False),
    RungDef(index=1, label="R1", elapsed_hours=2.0, wallclock_hour_ist=None,
            ceiling_pct=25.0, token_free=False),
    RungDef(index=2, label="R2", elapsed_hours=8.0, wallclock_hour_ist=16,
            ceiling_pct=50.0, token_free=False),
    RungDef(index=3, label="R3", elapsed_hours=None, wallclock_hour_ist=21,
            ceiling_pct=100.0, token_free=True),
]


def default_config(**overrides) -> MarkdownConfig:
    cfg = MarkdownConfig(rungs=DEFAULT_RUNGS)
    if overrides:
        cfg = cfg.model_copy(update=overrides)
    return cfg
