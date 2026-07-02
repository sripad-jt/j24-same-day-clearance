"""DeadStockClearanceWorkflow — durable, multi-day markdown for slow/dead stock.

One workflow per (store, jpin). Each nominal "day" (compressed by `demo_speed` for
demos) it: reads live on-hand + days-since-received, computes remaining shelf life
under the half-shelf-life assumption, asks the PURE `decide_deadstock` engine for
today's price (an escalating ramp keyed to days-to-expiry via `plan_clearance`),
gates deep cuts on owner approval (standing-rule auto-approve), applies the price
idempotently through Golden Eye, then sleeps to the next day and `continue_as_new`
to keep history bounded. Terminates when sold through, expired (cleared to floor),
or stopped.

Determinism boundary intact: posgateway/Bolt/parquet reads are in activities;
`decide_deadstock` is pure; the workflow derives day counts from `workflow.now()`.
Mirrors the same-day workflow's approval + idempotent-apply + sim patterns.
"""
from __future__ import annotations

import asyncio
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from activities.deadstock import (
        persist_deadstock_state,
        plan_deadstock_run,
        read_deadstock_stock,
    )
    from activities.persistence import persist_decision, record_run_event
    from activities.pipeline import (
        apply_price_goldeneye,
        notify_owner,
        publish_offer,
        request_owner_approval,
        shape_offer_llm,
    )
    from pricing.deadstock_engine import decide_deadstock
    from shared.models import (
        Approval,
        DeadStockState,
        ManualOverride,
        OwnerDecision,
        RunStatus,
        SimulateRequest,
        StandingRuleRequest,
    )

_READ = dict(start_to_close_timeout=timedelta(seconds=240),
             retry_policy=RetryPolicy(maximum_attempts=2))
_NOTIFY = dict(start_to_close_timeout=timedelta(seconds=30),
               retry_policy=RetryPolicy(maximum_attempts=5))
_APPLY = dict(start_to_close_timeout=timedelta(seconds=20),
              retry_policy=RetryPolicy(maximum_attempts=10))
_LLM = dict(start_to_close_timeout=timedelta(seconds=8),
            retry_policy=RetryPolicy(maximum_attempts=1))
_DB = dict(start_to_close_timeout=timedelta(seconds=15),
           retry_policy=RetryPolicy(maximum_attempts=5))

_MAX_DAYS_BEFORE_CAN = 14   # continue-as-new after this many day-ticks


def _rung_label(discount_pct: float) -> str:
    if discount_pct >= 99:
        return "R3"
    if discount_pct >= 50:
        return "R2"
    if discount_pct > 0:
        return "R1"
    return "R0"


@workflow.defn
class DeadStockClearanceWorkflow:
    def __init__(self) -> None:
        self._state: DeadStockState | None = None
        self._owner_decision: OwnerDecision | None = None
        self._stop = False
        self._sold_out = False
        self._sim: SimulateRequest | None = None   # sim: q0 = on_hand override
        self._sim_dirty = False
        self._price_seq = 0
        self._day = 0

    @workflow.run
    async def run(
        self,
        store_id: str,
        jpin: str,
        days_unsold: int = 0,
        shadow_mode: bool = False,
        demo_speed: float = 1800.0,
        simulate: bool = False,
        auto_apply: bool = True,
        standing_rule_pct: float = 100.0,
        mock_gateway: bool = False,
        # carried across continue-as-new:
        _price_seq: int = 0,
        _day: int = 0,
        _current_price: float | None = None,
    ) -> str:
        self._price_seq = _price_seq
        self._day = _day

        plan = await workflow.execute_activity(
            plan_deadstock_run,
            args=[store_id, jpin, days_unsold, shadow_mode, demo_speed, mock_gateway],
            **_READ,
        )
        run_id = workflow.info().workflow_id
        if not plan.eligible:
            await self._event(run_id, "SKIPPED", plan.skip_reason or "ineligible")
            return f"skipped: {plan.skip_reason}"

        cfg = plan.config
        start_price = _current_price if _current_price is not None else plan.list_price
        if self._state is None:
            self._state = DeadStockState(
                store_id=store_id, jpin=jpin, product_title=plan.product_title,
                category=plan.category, is_rte=plan.is_rte,
                status=RunStatus.OBSERVING,
                shelf_life_days=plan.shelf_life_days,
                days_since_received=plan.days_since_received,
                days_unsold=plan.days_unsold, on_hand=plan.on_hand,
                list_price=plan.list_price, current_price=start_price,
                floor_price=plan.floor_price,
                standing_rule_pct=standing_rule_pct, simulate=simulate,
            )
        if simulate and self._sim is None:
            self._sim = SimulateRequest(q0=plan.on_hand)
        await self._sync(run_id)
        await self._event(
            run_id, "STARTED",
            f"{plan.product_title} · on-hand {plan.on_hand} · shelf {plan.shelf_life_days}d "
            f"· unsold {plan.days_unsold}d · list ₹{plan.list_price:g} "
            f"· {'shadow' if shadow_mode else 'live'}",
        )

        day_seconds = max(1.0, 86400.0 / max(1.0, demo_speed))
        days_this_run = 0

        while not self._stop and not self._sold_out:
            # ---- read stock (sim overrides; else live Bolt) ----
            if self._state.simulate:
                self._sim_dirty = False
                sim = self._sim or SimulateRequest()
                if sim.q0 is not None:
                    self._state.on_hand = int(sim.q0)
                self._state.days_since_received += (1 if days_this_run else 0)
            else:
                stock = await workflow.execute_activity(
                    read_deadstock_stock, args=[store_id, jpin, mock_gateway], **_READ,
                )
                if stock.get("on_hand") is not None:
                    self._state.on_hand = int(stock["on_hand"])
                if stock.get("days_since_received") is not None:
                    self._state.days_since_received = int(stock["days_since_received"])

            if self._state.on_hand <= 0:
                self._sold_out = True
                break

            # ---- decide (pure) ----
            decision = decide_deadstock(
                on_hand=self._state.on_hand,
                days_unsold=self._state.days_unsold,
                shelf_life_days=self._state.shelf_life_days,
                days_since_received=self._state.days_since_received,
                list_price=self._state.list_price,
                floor_price=self._state.floor_price,
                current_price=self._state.current_price,
            )
            self._state.days_to_expiry = decision.days_to_expiry
            self._state.remaining_shelf_life_days = decision.remaining_shelf_life_days
            self._state.mode = decision.mode
            self._state.reorder_action = decision.reorder_action
            self._state.projected_days_to_clear = decision.projected_days_to_clear
            self._state.last_reason = decision.reason

            from_price = self._state.current_price
            steps_down = decision.target_price < from_price - 1e-9
            approval = Approval.NOT_REQUIRED
            apply_step = False

            if steps_down:
                needs_ok = (decision.requires_approval
                            and decision.discount_pct > self._state.standing_rule_pct
                            and not shadow_mode and not auto_apply)
                if needs_ok:
                    approval = await self._seek_approval(
                        run_id, plan.product_title, from_price, decision.target_price,
                        _rung_label(decision.discount_pct), cfg, demo_speed,
                    )
                    apply_step = approval == Approval.APPROVED
                else:
                    apply_step = True
            if shadow_mode:
                apply_step = False

            if apply_step:
                self._price_seq += 1
                to_rung = _rung_label(decision.discount_pct)
                confirmed = await workflow.execute_activity(
                    apply_price_goldeneye,
                    args=[run_id, store_id, jpin, to_rung, from_price,
                          decision.target_price, self._price_seq],
                    **_APPLY,
                )
                if not confirmed:
                    self._price_seq -= 1
                    await self._event(run_id, "APPLY_FAILED",
                                      f"Golden Eye did not confirm {to_rung}")
                else:
                    headline = await workflow.execute_activity(
                        shape_offer_llm,
                        args=[plan.product_title, decision.discount_pct, False,
                              cfg.enable_llm],
                        **_LLM,
                    )
                    await workflow.execute_activity(
                        publish_offer,
                        args=[run_id, store_id, jpin, headline,
                              decision.target_price, to_rung],
                        **_NOTIFY,
                    )
                    self._state.current_price = decision.target_price
                    self._state.current_discount_pct = decision.discount_pct
                    await self._event(run_id, "APPLIED",
                                      f"{to_rung} ₹{decision.target_price:g} — {headline}")

            await self._record(run_id, decision, approval)
            self._state.status = RunStatus.OBSERVING
            await self._sync(run_id)

            # ---- terminal: expired & cleared to floor ----
            if decision.days_to_expiry <= 0 and self._state.current_price <= self._state.floor_price + 1e-9:
                await self._event(run_id, "EXPIRED",
                                  f"expired at floor ₹{self._state.current_price:g}, "
                                  f"{self._state.on_hand} residual")
                break

            # ---- wait one nominal day (or an interrupting signal) ----
            try:
                await workflow.wait_condition(
                    lambda: self._stop or self._sold_out or self._sim_dirty,
                    timeout=timedelta(seconds=day_seconds),
                )
            except asyncio.TimeoutError:
                pass
            if self._stop or self._sold_out:
                break

            self._day += 1
            days_this_run += 1
            self._state.days_unsold += 1   # another day without clearing

            # ---- bound history via continue-as-new ----
            if days_this_run >= _MAX_DAYS_BEFORE_CAN:
                workflow.continue_as_new(args=[
                    store_id, jpin, self._state.days_unsold, shadow_mode, demo_speed,
                    self._state.simulate, auto_apply, self._state.standing_rule_pct,
                    mock_gateway,
                    self._price_seq, self._day, self._state.current_price,
                ])

        return await self._finalize(run_id)

    # ------------------------------------------------------------- helpers --- #
    async def _finalize(self, run_id: str) -> str:
        if self._sold_out:
            self._state.status = RunStatus.SOLD_OUT
            self._state.last_reason = "sold out / no on-hand stock"
        elif self._stop:
            self._state.status = RunStatus.STOPPED
            self._state.last_reason = self._state.last_reason or "stopped by owner"
        else:
            self._state.status = RunStatus.FINALIZED
        await self._event(run_id, self._state.status.value, self._state.last_reason)
        await self._sync(run_id)
        return self._state.last_reason

    async def _event(self, run_id: str, kind: str, message: str) -> None:
        await workflow.execute_activity(record_run_event, args=[run_id, kind, message], **_DB)

    async def _sync(self, run_id: str) -> None:
        await workflow.execute_activity(persist_deadstock_state, args=[run_id, self._state], **_DB)

    async def _record(self, run_id, decision, approval) -> None:
        await workflow.execute_activity(
            persist_decision,
            args=[run_id, {
                "rung": _rung_label(decision.discount_pct),
                "price": decision.target_price,
                "units_sold": max(0, self._state.on_hand),
                "run_rate": 0.0,
                "projected_clearance": decision.projected_days_to_clear,
                "residual": float(self._state.on_hand),
                "ratio": 0.0,
                "decision": "STEP" if decision.discount_pct > 0 else "HOLD",
                "approval": approval.value,
                "reason": decision.reason,
            }],
            **_DB,
        )

    async def _seek_approval(self, run_id, product, from_price, to_price,
                             to_rung, cfg, demo_speed) -> Approval:
        self._state.awaiting_approval = True
        self._state.pending_price = to_price
        self._state.status = RunStatus.AWAITING_APPROVAL
        self._owner_decision = None
        await self._sync(run_id)
        await workflow.execute_activity(
            request_owner_approval,
            args=[self._state.store_id, self._state.jpin, product, from_price,
                  to_price, self._state.on_hand, self._state.last_reason],
            **_NOTIFY,
        )
        await self._event(run_id, "AWAITING_APPROVAL",
                          f"{product}: ₹{from_price:g}→₹{to_price:g} ({to_rung})")
        timeout_s = cfg.approval_timeout_minutes * 60 / max(1.0, demo_speed)
        decided = True
        try:
            await workflow.wait_condition(
                lambda: self._owner_decision is not None or self._stop,
                timeout=timedelta(seconds=max(1.0, timeout_s)),
            )
        except asyncio.TimeoutError:
            decided = False
        self._state.awaiting_approval = False
        self._state.pending_price = None
        if not decided or self._owner_decision is None:
            await self._event(run_id, "TIMEOUT_HOLD", "no owner response — hold")
            return Approval.TIMEOUT_HOLD
        approve = self._owner_decision.approve
        self._owner_decision = None
        return Approval.APPROVED if approve else Approval.REJECTED

    # --------------------------------------------------------------- signals --- #
    @workflow.signal
    def owner_decision(self, decision: OwnerDecision) -> None:
        self._owner_decision = decision

    @workflow.signal
    def sold_out(self) -> None:
        self._sold_out = True

    @workflow.signal
    def manual_override(self, ov: ManualOverride) -> None:
        if ov.action == "stop":
            self._stop = True

    @workflow.signal
    def set_standing_rule(self, req: StandingRuleRequest) -> None:
        if self._state is not None:
            self._state.standing_rule_pct = req.auto_approve_max_discount_pct

    @workflow.signal
    def simulate(self, req: SimulateRequest) -> None:
        """Sim mode: `q0` sets the current on-hand (drive stock down day-by-day)."""
        cur = self._sim or SimulateRequest()
        self._sim = SimulateRequest(
            units_sold=req.units_sold if req.units_sold is not None else cur.units_sold,
            recent_rate=req.recent_rate if req.recent_rate is not None else cur.recent_rate,
            q0=req.q0 if req.q0 is not None else cur.q0,
        )
        self._sim_dirty = True

    @workflow.query
    def current_state(self) -> DeadStockState | None:
        return self._state
