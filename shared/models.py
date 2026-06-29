"""Shared Pydantic models — the contract between workflow, activities, and API.

These are passed across the Temporal boundary, so they must be JSON-serialisable
(we use the temporalio pydantic data converter). Keep them free of behaviour.
"""
from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


# --------------------------------------------------------------------------- #
# Enums
# --------------------------------------------------------------------------- #
class Decision(str, Enum):
    HOLD = "HOLD"
    STEP = "STEP"
    AUTO_CLEAR = "AUTO_CLEAR"


class Approval(str, Enum):
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    NOT_REQUIRED = "NOT_REQUIRED"
    TIMEOUT_HOLD = "TIMEOUT_HOLD"
    PENDING = "PENDING"


class RunStatus(str, Enum):
    STARTED = "STARTED"
    OBSERVING = "OBSERVING"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    APPLYING = "APPLYING"
    FINALIZED = "FINALIZED"
    SOLD_OUT = "SOLD_OUT"
    STOPPED = "STOPPED"


class ClearanceMode(str, Enum):
    HOLD = "HOLD"
    NUDGE = "NUDGE"
    CLEAR_MULTIDAY = "CLEAR_MULTIDAY"
    CLEAR_SAMEDAY = "CLEAR_SAMEDAY"
    SUPPRESS_REORDER = "SUPPRESS_REORDER"


class ReorderAction(str, Enum):
    NONE = "NONE"
    REDUCE_OTB = "REDUCE_OTB"
    STOP_REORDER = "STOP_REORDER"


# --------------------------------------------------------------------------- #
# Configuration (snapshotted into workflow state at run start)
# --------------------------------------------------------------------------- #
class RungDef(BaseModel):
    """One step on the markdown ladder — kept as display labels and backstops."""

    index: int
    label: str
    elapsed_hours: Optional[float]
    wallclock_hour_ist: Optional[int]
    ceiling_pct: float
    token_free: bool = False


class MarkdownConfig(BaseModel):
    rungs: list[RungDef]
    # sell-through
    trailing_window_hours: float = 1.5
    min_q0: int = 5
    giveaway_alert_qty: int = 50
    # approval
    approval_timeout_minutes: int = 30
    # store timing
    rte_autoclear_gate_hour: int = 20   # IST; RTE after this auto-clears to ₹1
    store_close_hour: int = 21          # IST
    token_free_price: float = 1.0
    # decision engine v2 params
    elasticity: float = 0.6            # demand lift per unit relative price cut
    max_discount_pct: float = 60.0     # policy cap before the token rung
    step_pct: float = 5.0              # discount granularity off list
    hysteresis_units: float = 1.0      # don't step for residual smaller than this
    residual_tolerance: float = 0.0    # acceptable end-of-day leftover
    # continuous loop
    poll_interval_min: float = 30.0    # how often to evaluate per nominal hour
    measure_window_h: float = 1.5      # how long after offer to measure lift
    # misc
    enable_llm: bool = False
    shadow_mode: bool = False
    demo_speed: float = 1.0


# --------------------------------------------------------------------------- #
# Run inputs / data acquisition
# --------------------------------------------------------------------------- #
class ReceiptContext(BaseModel):
    """Stage B output — per-batch facts from the Inventory Item Details API."""

    store_id: str
    jpin: str
    receipt_date: str
    product_title: str
    category: str
    is_rte: bool
    shelf_life_days: int
    q0: int
    q0_source: str = "synthetic"       # lot_initial_qty | synthetic
    list_price: float
    mrp: float
    received_epoch_ms: int
    mfg_date: Optional[str] = None
    expiry_date: Optional[str] = None


class SellThroughV2(BaseModel):
    """fetch_sellthrough output — today-bounded units + trailing rate.

    Key differences from the old SellThrough:
      - units_sold_today is OUTWARDED since T0 today (not a 24-47h lookback)
      - recent_rate is the trailing window rate (reacts to markdown lift)
      - q0 is read live from initialQty of today's lots (not synthetic)
      - q0_source records the confidence level
    """

    units_sold_today: int
    recent_rate: float                 # units/hour, trailing window
    cumulative_rate: float = 0.0       # display only; never feeds the projection
    q0: int = 0
    q0_source: str = "synthetic"       # lot_initial_qty | synthetic
    window_h: float = 0.0
    low_confidence: bool = False


class RunPlan(BaseModel):
    """plan_run output — everything the (deterministic) workflow needs up front."""

    receipt: ReceiptContext
    config: MarkdownConfig
    close_offset_s: float              # seconds from run start to store close
    floor_price: float = 0.0          # cost / salvage clamp per JPIN
    eligible: bool
    skip_reason: Optional[str] = None


# --------------------------------------------------------------------------- #
# Decision engine output (pure)
# --------------------------------------------------------------------------- #
class PriceDecisionV2(BaseModel):
    """Output of decide_v2() — a price, not a rung index.

    Demand-gated: driven by the projected end-of-day residual and an elasticity
    estimate, clamped to a price floor. Rungs are display labels only.
    """

    decision: Decision
    target_price: float
    discount_pct: float                # off list, for the offer headline
    reason: str
    residual_at_current: float         # units left over if we don't step
    residual_at_target: float          # units left over at the chosen price
    projected_clearance_at_target: float
    ratio: float                       # proj_at_current / q0
    clears: bool                       # target projected to clear by close?
    floored: bool                      # did we hit the price floor without clearing?
    requires_approval: bool


# --------------------------------------------------------------------------- #
# Shelf-life scheduler output (pure)
# --------------------------------------------------------------------------- #
class ShelfLifePlan(BaseModel):
    """Output of plan_clearance() — runs once/day per multi-day batch.

    For L=1 lines this collapses to CLEAR_SAMEDAY on the receipt date.
    """

    mode: ClearanceMode
    recommended_discount_pct: float
    is_terminal_day: bool
    projected_days_to_clear: float
    days_to_expiry: int
    clearance_window_days: int
    reorder_action: ReorderAction
    reason: str


# --------------------------------------------------------------------------- #
# Workflow state (exposed via the current_state query)
# --------------------------------------------------------------------------- #
class HistoryEntry(BaseModel):
    ts_ist: str
    rung: str
    price: float
    units_sold: int
    ratio: float
    decision: Decision
    approval: Approval
    reason: str
    source: str = "poll"               # poll | override | grn | soldout


class MarkdownState(BaseModel):
    # identity
    store_id: str
    jpin: str
    receipt_date: str
    clearance_date: str
    product_title: str
    category: str
    is_rte: bool
    # pricing
    list_price: float
    mrp: float
    current_rung: str                  # display label (R0-R3)
    current_price: float
    floor_price: float = 0.0
    # sell-through
    q0: int
    q0_source: str = "synthetic"
    units_sold: int = 0
    recent_rate: float = 0.0           # trailing units/hour
    projected_clearance: float = 0.0
    residual: float = 0.0
    ratio: float = 0.0
    # decision v2 diagnostics
    clears: bool = True
    floored: bool = False
    # clearance mode (from shelf-life scheduler)
    clearance_mode: str = "CLEAR_SAMEDAY"
    reorder_action: str = "NONE"
    # control
    status: RunStatus = RunStatus.STARTED
    awaiting_approval: bool = False
    shadow_mode: bool = False
    pending_rung: Optional[str] = None
    pending_price: Optional[float] = None
    low_confidence: bool = False
    standing_rule_pct: float = 100.0   # auto-approve ceiling for the day
    last_reason: str = ""
    history: list[HistoryEntry] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Audit (immutable record)
# --------------------------------------------------------------------------- #
class AuditEvent(BaseModel):
    run_id: str
    store_id: str
    jpin: str
    ts_ist: str
    from_rung: str
    to_rung: str
    from_price: float
    to_price: float
    q0: int
    units_sold: int
    recent_rate: float
    projected_clearance: float
    residual: float
    ratio: float
    clears: bool
    floored: bool
    decision: Decision
    approval: Approval
    reason: str


# --------------------------------------------------------------------------- #
# Owner-education feed (pre/post sell-through of an offer)
# --------------------------------------------------------------------------- #
class OfferBaseline(BaseModel):
    """Snapshot captured at the instant a markdown is applied — the 'before'."""

    run_id: str
    store_id: str
    jpin: str
    product_title: str
    rung: str
    from_price: float
    to_price: float
    discount_pct: float
    ts_ist: str
    units_sold_before: int
    rate_before: float
    units_left_before: int


class OfferOutcome(BaseModel):
    """The 'after' + computed lift, pushed to the Giant as an education card."""

    run_id: str
    store_id: str
    jpin: str
    product_title: str
    rung: str
    price: float
    discount_pct: float
    ts_ist: str
    phase: str                         # "interim" | "final"
    rate_before: float
    rate_after: float
    lift_pct: float
    units_sold_after: int
    incremental_units: float
    units_left: int
    revenue_recovered: float
    waste_avoided_units: int
    waste_avoided_value: float
    headline: str


# --------------------------------------------------------------------------- #
# Signals / API payloads
# --------------------------------------------------------------------------- #
class OwnerDecision(BaseModel):
    rung: str
    approve: bool
    note: str = ""


class AdditionalGrn(BaseModel):
    qty: int
    note: str = ""


class ManualOverride(BaseModel):
    action: str                        # "force_rung" | "stop"
    rung: Optional[str] = None


class StandingRuleRequest(BaseModel):
    auto_approve_max_discount_pct: float


class SeedRequest(BaseModel):
    """API helper to start markdown runs for a store."""

    count: int = 3
    store_id: str = "BZID-1304298141"
    shadow_mode: bool = False
    demo_speed: float = 1800.0
    include_rte: bool = True
    jpins: Optional[list[str]] = None
