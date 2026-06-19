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


# --------------------------------------------------------------------------- #
# Configuration (snapshotted into workflow state at run start — §8 of design)
# --------------------------------------------------------------------------- #
class RungDef(BaseModel):
    """One step on the markdown ladder."""

    index: int                       # 0..N, monotonic
    label: str                       # R0, R1, R2, R3
    elapsed_hours: Optional[float]   # trigger: hours after T0 (None for R0)
    wallclock_hour_ist: Optional[int]  # trigger: IST hour (whichever comes first)
    ceiling_pct: float               # max markdown at this rung (0, 25, 50)
    token_free: bool = False         # R3 -> token ₹1


class MarkdownConfig(BaseModel):
    rungs: list[RungDef]
    theta_hold: float = 0.85
    trailing_window_hours: float = 1.5
    min_q0: int = 5                  # below this, do not run
    giveaway_alert_qty: int = 50     # alert if more than this given away in a day
    approval_timeout_minutes: int = 30
    rte_autoclear_gate_hour: int = 20   # IST; RTE after this auto-clears to ₹1
    store_close_hour: int = 21          # IST
    token_free_price: float = 1.0
    enable_llm: bool = False
    shadow_mode: bool = False
    # Demo: compress nominal hours into seconds. 1.0 = real time.
    # demo_speed=3600 -> 1 nominal hour elapses in 1 second.
    demo_speed: float = 1.0


# --------------------------------------------------------------------------- #
# Run inputs / data acquisition (§4 of design)
# --------------------------------------------------------------------------- #
class ReceiptContext(BaseModel):
    """Stage B output — per-batch facts from the Inventory Item Details API."""

    store_id: str
    jpin: str
    receipt_date: str               # ISO date
    product_title: str
    category: str
    is_rte: bool
    shelf_life_days: int            # L
    q0: int                         # opening stock at T0
    list_price: float               # listing selling price
    mrp: float
    received_epoch_ms: int          # inventoryItemCreatedTime
    mfg_date: Optional[str] = None
    expiry_date: Optional[str] = None


class SellThrough(BaseModel):
    """fetch_sellthrough output — derived from active + OUTWARDED states / POS."""

    units_sold: int
    run_rate: float                 # units / nominal hour
    low_confidence: bool = False    # POS stale -> fell back to all-day average


class Checkpoint(BaseModel):
    """A ladder checkpoint, pre-planned by the plan_run activity.

    Timing is expressed as an offset in *seconds* from run start (already scaled
    by demo_speed). The decision engine uses the nominal hours, never the seconds.
    """

    rung_index: int
    label: str
    sleep_offset_s: float           # seconds from run start to this checkpoint
    nominal_elapsed_h: float        # hours since T0 (for the engine)
    nominal_remaining_h: float      # hours to must-clear (for the engine)
    ceiling_pct: float
    token_free: bool
    wallclock_hour_ist: int         # the IST hour this checkpoint represents


class RunPlan(BaseModel):
    """plan_run output — everything the (deterministic) workflow needs up front."""

    receipt: ReceiptContext
    config: MarkdownConfig
    checkpoints: list[Checkpoint]
    close_offset_s: float           # seconds from start to store close
    eligible: bool                  # False if q0 < min_q0 or list_price missing
    skip_reason: Optional[str] = None


# --------------------------------------------------------------------------- #
# Decision engine output (pure, §8)
# --------------------------------------------------------------------------- #
class DecisionResult(BaseModel):
    target_rung_index: int
    decision: Decision
    reason: str
    ratio: float
    projected_clearance: float
    residual: float
    target_price: float
    requires_approval: bool


# --------------------------------------------------------------------------- #
# Workflow state (exposed via the current_state query — §6)
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
    source: str = "checkpoint"      # checkpoint | override | grn | soldout


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
    current_rung: str
    current_price: float
    # sell-through
    q0: int
    units_sold: int = 0
    run_rate: float = 0.0
    projected_clearance: float = 0.0
    residual: float = 0.0
    ratio: float = 0.0
    # control
    status: RunStatus = RunStatus.STARTED
    awaiting_approval: bool = False
    shadow_mode: bool = False
    pending_rung: Optional[str] = None
    pending_price: Optional[float] = None
    low_confidence: bool = False
    last_reason: str = ""
    history: list[HistoryEntry] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Audit (immutable record — §10)
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
    run_rate: float
    projected_clearance: float
    residual: float
    ratio: float
    decision: Decision
    approval: Approval
    reason: str


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
    action: str                     # "force_rung" | "stop"
    rung: Optional[str] = None


class SeedRequest(BaseModel):
    """API helper to start synthetic demo runs."""

    count: int = 3
    store_id: str = "BTMLayout"
    shadow_mode: bool = False
    demo_speed: float = 1800.0      # 1 nominal hour -> 2 seconds
    include_rte: bool = True
