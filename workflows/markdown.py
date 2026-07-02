"""PerishableMarkdownWorkflow — the durable, day-long markdown agent (v2).

v2 changes from v1:
  - Continuous poll loop instead of 4 pre-planned checkpoints
  - Shelf-life scheduler (plan_clearance) called at start to classify the line
  - decide_v2() drives markdowns on projected residual, not clock rungs
  - Standing-rule auto-approve: owner sets a daily discount ceiling; steps
    within it are applied without a card, deeper cuts still require consent
  - Owner-education feed: capture_offer_baseline + deferred measure_offer_outcome
  - Idempotency key is (run_id, price_seq) — a monotonic counter per run

Determinism boundary is preserved: workflows orchestrate, activities do I/O.
All time-of-day values arrive as computed parameters; no clock in the workflow.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from activities.feeds import capture_offer_baseline, measure_offer_outcome
    from activities.persistence import persist_decision, persist_state, record_run_event
    from activities.pipeline import (
        apply_price_goldeneye,
        fetch_sellthrough,
        notify_owner,
        plan_run,
        publish_offer,
        request_owner_approval,
        shape_offer_llm,
        write_audit,
    )
    from activities.profile import resolve_intraday_profile
    from activities.snapshot import read_snapshot
    from pricing.decision_engine import decide_v2, decide_v3, price_for_rung
    from pricing.projection import project_remaining_demand
    from pricing.shelf_life_scheduler import plan_clearance
    from shared.models import (
        AdditionalGrn,
        Approval,
        AuditEvent,
        ClearanceMode,
        Decision,
        ManualOverride,
        MarkdownState,
        OfferBaseline,
        OwnerDecision,
        PriceDecisionV2,
        RunStatus,
        SellThroughV2,
        SimulateRequest,
        StandingRuleRequest,
    )
    from shared.stores import get_store

_T0_HOUR_IST = 8


# Activity option presets (retry/timeout matrix)
_READ = dict(
    start_to_close_timeout=timedelta(seconds=240),
    retry_policy=RetryPolicy(maximum_attempts=2),
)
_NOTIFY = dict(
    start_to_close_timeout=timedelta(seconds=30),
    retry_policy=RetryPolicy(maximum_attempts=5),
)
_APPLY = dict(
    start_to_close_timeout=timedelta(seconds=20),
    retry_policy=RetryPolicy(maximum_attempts=10),
)
_LLM = dict(
    start_to_close_timeout=timedelta(seconds=8),
    retry_policy=RetryPolicy(maximum_attempts=1),
)
_DB = dict(
    start_to_close_timeout=timedelta(seconds=15),
    retry_policy=RetryPolicy(maximum_attempts=5),
)


def _rung_label(discount_pct: float, token_free: bool = False) -> str:
    if token_free or discount_pct >= 99:
        return "R3"
    if discount_pct >= 50:
        return "R2"
    if discount_pct > 0:
        return "R1"
    return "R0"


def _t0_epoch_ms(receipt_date: str) -> int:
    """T0 = receipt_date at 08:00 IST = 02:30 UTC, as epoch-ms."""
    dt = datetime.strptime(receipt_date, "%Y-%m-%d").replace(
        hour=2, minute=30, tzinfo=timezone.utc
    )
    return int(dt.timestamp() * 1000)


def _days_to_expiry(expiry_date: str | None, receipt_date: str) -> int:
    from datetime import date
    target = date.fromisoformat(expiry_date or receipt_date)
    today = date.fromisoformat(receipt_date)
    return max(0, (target - today).days)


def _dow(receipt_date: str) -> int:
    """Day-of-week (0=Mon..6=Sun) of the clearance day — pure/deterministic."""
    from datetime import date
    return date.fromisoformat(receipt_date).weekday()


@workflow.defn
class PerishableMarkdownWorkflow:
    def __init__(self) -> None:
        self._state: MarkdownState | None = None
        self._owner_decision: OwnerDecision | None = None
        self._pending_grn: int = 0
        self._sold_out: bool = False
        self._stop: bool = False
        self._force_rung_label: str | None = None
        self._sim: SimulateRequest | None = None   # set in sim mode; UI-driven sell-through
        self._sim_dirty: bool = False              # a sim edit arrived → re-decide now

    # ----------------------------------------------------------------- run --- #
    @workflow.run
    async def run(
        self,
        store_id: str,
        jpin: str,
        receipt_date: str,
        shadow_mode: bool = False,
        demo_speed: float = 1800.0,
        simulate: bool = False,
    ) -> str:
        plan = await workflow.execute_activity(
            plan_run,
            args=[store_id, jpin, receipt_date, shadow_mode, demo_speed],
            **_READ,
        )
        r = plan.receipt
        run_id = workflow.info().workflow_id

        if not plan.eligible:
            await self._event(run_id, "SKIPPED", plan.skip_reason or "ineligible")
            return f"skipped: {plan.skip_reason}"

        cfg = plan.config
        total_h = float(cfg.store_close_hour - _T0_HOUR_IST)
        t0_ms = _t0_epoch_ms(receipt_date)
        facility_id = (get_store(store_id) or {}).get("facility_id", "") or ""

        # Shelf-life scheduler: classify this line's clearance mode for today
        days_to_exp = _days_to_expiry(r.expiry_date, receipt_date)
        daily_rate = max(r.q0 / max(r.shelf_life_days, 1), 0.5)
        shelf_plan = plan_clearance(
            shelf_life_days=r.shelf_life_days,
            days_to_expiry=days_to_exp,
            remaining_units=r.q0,
            daily_rate=daily_rate,
        )

        self._state = MarkdownState(
            store_id=store_id, jpin=jpin, receipt_date=receipt_date,
            clearance_date=receipt_date,
            product_title=r.product_title, category=r.category, is_rte=r.is_rte,
            list_price=r.list_price, mrp=r.mrp,
            current_rung="R0", current_price=r.list_price,
            floor_price=plan.floor_price,
            q0=r.q0, q0_source=r.q0_source,
            clearance_mode=shelf_plan.mode.value,
            reorder_action=shelf_plan.reorder_action.value,
            status=RunStatus.OBSERVING, shadow_mode=shadow_mode,
            simulate=simulate,
        )
        # Sim mode: seed sell-through from the live Q0 (price is already live from
        # plan_run); the operator then drives units_sold/rate/Q0 from the UI.
        if simulate:
            self._sim = SimulateRequest(units_sold=0, recent_rate=0.0, q0=r.q0)
        await self._sync(run_id)
        await self._event(
            run_id, "STARTED",
            f"{r.product_title} · Q0={r.q0} ({r.q0_source}) · "
            f"list ₹{r.list_price:g} · mode={shelf_plan.mode.value} · "
            f"{'shadow' if shadow_mode else 'live'}",
        )

        # Non-clearance modes: log and exit today's loop
        if shelf_plan.mode in (ClearanceMode.HOLD, ClearanceMode.NUDGE):
            await self._event(run_id, shelf_plan.mode.value, shelf_plan.reason)
            self._state.status = RunStatus.FINALIZED
            self._state.last_reason = shelf_plan.reason
            await self._sync(run_id)
            return shelf_plan.reason

        if shelf_plan.reorder_action.value != "NONE":
            await self._event(run_id, "REORDER_SIGNAL",
                              f"{shelf_plan.reorder_action.value}: {shelf_plan.reason}")

        # Continuous poll loop
        start = workflow.now()
        price_seq = 0

        while not self._stop and not self._sold_out:
            elapsed_s = (workflow.now() - start).total_seconds()
            nominal_elapsed_h = elapsed_s * demo_speed / 3600.0
            remaining_h = max(0.0, total_h - nominal_elapsed_h)
            if remaining_h <= 0:
                break

            # Handle pending GRN signals
            if self._pending_grn:
                self._state.q0 += self._pending_grn
                await self._event(run_id, "GRN",
                                  f"+{self._pending_grn} re-received; Q0={self._state.q0}")
                self._pending_grn = 0

            # Handle forced rung override
            forced_price = self._consume_forced_price(r.list_price, cfg)

            # Wait for next poll tick (or an interrupting signal)
            poll_s = max(1.0, cfg.poll_interval_min * 60.0 / demo_speed)
            try:
                await workflow.wait_condition(
                    lambda: bool(self._pending_grn or self._force_rung_label
                                 or self._stop or self._sold_out or self._sim_dirty),
                    timeout=timedelta(seconds=poll_s),
                )
            except asyncio.TimeoutError:
                pass

            if self._stop or self._sold_out:
                break

            # Re-compute elapsed after waiting
            elapsed_s = (workflow.now() - start).total_seconds()
            nominal_elapsed_h = elapsed_s * demo_speed / 3600.0
            remaining_h = max(0.0, total_h - nominal_elapsed_h)
            if remaining_h <= 0:
                break

            current_pct = (
                round((1 - self._state.current_price / self._state.list_price) * 100, 1)
                if self._state.list_price else 0.0
            )
            if self._state.simulate:
                # Sim mode: sell-through comes from the operator (UI signal), not
                # Bolt — this avoids the slow OUTWARDED read while keeping the live
                # price anchor. rate defaults to the average over the nominal day.
                self._sim_dirty = False
                sim = self._sim or SimulateRequest()
                sim_units = int(sim.units_sold or 0)
                sim_q0 = int(sim.q0) if sim.q0 is not None else self._state.q0
                sim_rate = (
                    float(sim.recent_rate) if sim.recent_rate is not None
                    else sim_units / max(nominal_elapsed_h, 0.5)
                )
                st = SellThroughV2(
                    units_sold_today=sim_units,
                    recent_rate=round(sim_rate, 3),
                    q0=sim_q0,
                    q0_source="sim",
                    window_h=max(0.5, nominal_elapsed_h),
                    low_confidence=False,
                )
            elif cfg.read_from_snapshot:
                st = await workflow.execute_activity(
                    read_snapshot,
                    args=[store_id, jpin, receipt_date, self._state.q0],
                    **_READ,
                )
            else:
                st = await workflow.execute_activity(
                    fetch_sellthrough,
                    args=[store_id, jpin, self._state.q0, t0_ms,
                          cfg.trailing_window_hours, current_pct],
                    **_READ,
                )
            self._state.units_sold = st.units_sold_today
            self._state.recent_rate = st.recent_rate
            # Live/snapshot Q0 only moves up (GRN-safe); sim Q0 is set exactly.
            self._state.q0 = st.q0 if self._state.simulate else max(self._state.q0, st.q0)
            self._state.q0_source = st.q0_source
            self._state.low_confidence = st.low_confidence

            # Re-compute after activity latency
            elapsed_s = (workflow.now() - start).total_seconds()
            nominal_elapsed_h = elapsed_s * demo_speed / 3600.0
            remaining_h = max(0.0, total_h - nominal_elapsed_h)
            past_rte_gate = nominal_elapsed_h >= (cfg.rte_autoclear_gate_hour - _T0_HOUR_IST)
            token_eligible = remaining_h < 1.0

            # Decision
            if forced_price is not None:
                discount_pct = (
                    round((1 - forced_price / r.list_price) * 100, 1) if r.list_price else 0.0
                )
                decision = PriceDecisionV2(
                    decision=Decision.STEP,
                    target_price=forced_price,
                    discount_pct=discount_pct,
                    reason=f"manual override to ₹{forced_price:g}",
                    residual_at_current=max(0, self._state.q0 - st.units_sold_today),
                    residual_at_target=0.0,
                    projected_clearance_at_target=float(self._state.q0),
                    ratio=st.units_sold_today / max(1, self._state.q0),
                    clears=True, floored=False, requires_approval=False,
                )
            else:
                if cfg.projection_mode == "v3":
                    nominal_hour_ist = _T0_HOUR_IST + nominal_elapsed_h
                    prof = await workflow.execute_activity(
                        resolve_intraday_profile,
                        args=[store_id, jpin, _dow(receipt_date),
                              int(nominal_hour_ist), nominal_hour_ist % 1.0,
                              _T0_HOUR_IST, cfg.store_close_hour],
                        **_READ,
                    )
                    proj = project_remaining_demand(
                        units_sold=st.units_sold_today,
                        recent_rate=st.recent_rate,
                        remaining_h=remaining_h,
                        cum_share_to_now=prof.cum_share_to_now,
                        remaining_share=prof.remaining_share,
                        profile_source=prof.source_level,
                        share_ref=cfg.profile_share_ref,
                        units_ref=cfg.profile_units_ref,
                    )
                    decision = decide_v3(
                        q0=self._state.q0,
                        units_sold=st.units_sold_today,
                        remaining_demand=proj.remaining_demand,
                        current_price=self._state.current_price,
                        list_price=r.list_price,
                        floor_price=plan.floor_price,
                        elasticity=cfg.elasticity,
                        token_free_price=cfg.token_free_price,
                        residual_tolerance=cfg.residual_tolerance,
                        step_pct=cfg.step_pct,
                        max_discount_pct=cfg.max_discount_pct,
                        hysteresis_units=cfg.hysteresis_units,
                        is_rte=r.is_rte,
                        past_rte_gate=past_rte_gate,
                        token_eligible=token_eligible,
                        projection_method=proj.method,
                    )
                else:
                    decision = decide_v2(
                        q0=self._state.q0,
                        units_sold=st.units_sold_today,
                        recent_rate=st.recent_rate,
                        remaining_h=remaining_h,
                        current_price=self._state.current_price,
                        list_price=r.list_price,
                        floor_price=plan.floor_price,
                        elasticity=cfg.elasticity,
                        token_free_price=cfg.token_free_price,
                        residual_tolerance=cfg.residual_tolerance,
                        step_pct=cfg.step_pct,
                        max_discount_pct=cfg.max_discount_pct,
                        hysteresis_units=cfg.hysteresis_units,
                        is_rte=r.is_rte,
                        past_rte_gate=past_rte_gate,
                        token_eligible=token_eligible,
                    )

            self._state.projected_clearance = decision.projected_clearance_at_target
            self._state.residual = decision.residual_at_current
            self._state.ratio = decision.ratio
            self._state.clears = decision.clears
            self._state.floored = decision.floored
            self._state.last_reason = decision.reason

            from_rung = self._state.current_rung
            from_price = self._state.current_price
            apply_step = False
            approval = Approval.NOT_REQUIRED

            if decision.decision in (Decision.STEP, Decision.AUTO_CLEAR):
                if (decision.requires_approval
                        and decision.discount_pct > self._state.standing_rule_pct
                        and not shadow_mode):
                    approval = await self._seek_approval(
                        run_id, r.product_title, from_price, decision.target_price,
                        _rung_label(decision.discount_pct,
                                    decision.decision == Decision.AUTO_CLEAR),
                        cfg, demo_speed,
                    )
                    apply_step = (approval == Approval.APPROVED)
                else:
                    apply_step = True   # within standing rule or AUTO_CLEAR or shadow

            if shadow_mode:
                apply_step = False

            if apply_step:
                price_seq += 1
                to_rung = _rung_label(decision.discount_pct,
                                      decision.decision == Decision.AUTO_CLEAR)
                confirmed = await workflow.execute_activity(
                    apply_price_goldeneye,
                    args=[run_id, store_id, jpin, to_rung, from_price,
                          decision.target_price, price_seq],
                    **_APPLY,
                )
                if not confirmed:
                    await self._event(run_id, "APPLY_FAILED",
                                      f"Golden Eye did not confirm {to_rung}")
                    price_seq -= 1   # roll back — not applied
                else:
                    pct_off = decision.discount_pct
                    headline = await workflow.execute_activity(
                        shape_offer_llm,
                        args=[r.product_title, pct_off,
                              decision.decision == Decision.AUTO_CLEAR, cfg.enable_llm],
                        **_LLM,
                    )
                    await workflow.execute_activity(
                        publish_offer,
                        args=[run_id, store_id, jpin, headline,
                              decision.target_price, to_rung],
                        **_NOTIFY,
                    )
                    self._state.current_rung = to_rung
                    self._state.current_price = decision.target_price
                    await self._event(run_id, "APPLIED",
                                      f"{to_rung} ₹{decision.target_price:g} — {headline}")

                    # Owner-education feed: capture baseline + schedule measurement
                    baseline = OfferBaseline(
                        run_id=run_id, store_id=store_id, jpin=jpin,
                        product_title=r.product_title, rung=to_rung,
                        from_price=from_price, to_price=decision.target_price,
                        discount_pct=pct_off,
                        ts_ist=workflow.now().isoformat(),
                        units_sold_before=st.units_sold_today,
                        rate_before=st.recent_rate,
                        units_left_before=max(0, self._state.q0 - st.units_sold_today),
                    )
                    await workflow.execute_activity(
                        capture_offer_baseline, args=[baseline], **_DB
                    )
                    measure_sleep_s = cfg.measure_window_h * 3600.0 / demo_speed
                    asyncio.create_task(
                        self._measure_later(
                            baseline, measure_sleep_s, t0_ms,
                            cfg.measure_window_h, facility_id,
                        )
                    )

                    if decision.decision == Decision.AUTO_CLEAR and r.is_rte:
                        await workflow.execute_activity(
                            notify_owner,
                            args=[store_id,
                                  f"{r.product_title}: RTE auto-cleared to "
                                  f"₹{decision.target_price:g} at close."],
                            **_NOTIFY,
                        )

            await self._record(run_id, _rung_label(decision.discount_pct, False),
                               decision, approval, st.units_sold_today, st.recent_rate)
            await self._audit(run_id, from_rung, from_price, decision, approval,
                              st.units_sold_today, st.recent_rate)
            self._state.status = RunStatus.OBSERVING
            await self._sync(run_id)

        return await self._finalize(run_id)

    # ------------------------------------------------------ measurement task --- #
    async def _measure_later(
        self,
        baseline: OfferBaseline,
        sleep_s: float,
        t0_ms: int,
        window_h: float,
        facility_id: str,
    ) -> None:
        """Deferred coroutine: sleep then measure the post-offer lift."""
        await workflow.sleep(timedelta(seconds=max(1.0, sleep_s)))
        if self._state is None:
            return
        ts_ist = workflow.now().isoformat()
        try:
            await workflow.execute_activity(
                measure_offer_outcome,
                args=[baseline, baseline.store_id, baseline.jpin, facility_id,
                      t0_ms, window_h, ts_ist, "interim", 0.0],
                **_NOTIFY,
            )
        except Exception:  # noqa: BLE001 — never block the main loop
            pass

    # --------------------------------------------------------------- helpers --- #
    def _consume_forced_price(self, list_price: float, cfg) -> float | None:
        if self._force_rung_label is None:
            return None
        label = self._force_rung_label
        self._force_rung_label = None
        for rung in cfg.rungs:
            if rung.label == label:
                return price_for_rung(rung, list_price, cfg.token_free_price)
        return None

    async def _seek_approval(self, run_id, product, from_price, to_price,
                              to_rung, cfg, demo_speed) -> Approval:
        self._state.awaiting_approval = True
        self._state.pending_rung = to_rung
        self._state.pending_price = to_price
        self._state.status = RunStatus.AWAITING_APPROVAL
        self._owner_decision = None
        await self._sync(run_id)
        await workflow.execute_activity(
            request_owner_approval,
            args=[self._state.store_id, self._state.jpin, product, from_price,
                  to_price, max(0, self._state.q0 - self._state.units_sold),
                  self._state.last_reason],
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
        self._state.pending_rung = None
        self._state.pending_price = None

        if not decided or self._owner_decision is None:
            await self._event(run_id, "TIMEOUT_HOLD",
                              "no owner response — hold at current price")
            return Approval.TIMEOUT_HOLD
        approve = self._owner_decision.approve
        self._owner_decision = None
        return Approval.APPROVED if approve else Approval.REJECTED

    async def _record(self, run_id, rung_label, decision: PriceDecisionV2,
                      approval, units_sold, recent_rate) -> None:
        await workflow.execute_activity(
            persist_decision,
            args=[run_id, {
                "rung": rung_label,
                "price": decision.target_price,
                "units_sold": units_sold,
                "run_rate": recent_rate,
                "projected_clearance": decision.projected_clearance_at_target,
                "residual": decision.residual_at_current,
                "ratio": decision.ratio,
                "decision": decision.decision.value,
                "approval": approval.value,
                "reason": decision.reason,
            }],
            **_DB,
        )

    async def _audit(self, run_id, from_rung, from_price, decision: PriceDecisionV2,
                     approval, units_sold, recent_rate) -> None:
        ev = AuditEvent(
            run_id=run_id, store_id=self._state.store_id, jpin=self._state.jpin,
            ts_ist=workflow.now().isoformat(),
            from_rung=from_rung,
            to_rung=_rung_label(decision.discount_pct,
                                decision.decision == Decision.AUTO_CLEAR),
            from_price=from_price, to_price=decision.target_price,
            q0=self._state.q0, units_sold=units_sold, recent_rate=recent_rate,
            projected_clearance=decision.projected_clearance_at_target,
            residual=decision.residual_at_current,
            ratio=decision.ratio,
            clears=decision.clears, floored=decision.floored,
            decision=decision.decision, approval=approval,
            reason=decision.reason,
        )
        await workflow.execute_activity(write_audit, args=[ev], **_DB)

    async def _finalize(self, run_id: str) -> str:
        if self._sold_out:
            self._state.status = RunStatus.SOLD_OUT
            self._state.last_reason = "line sold out — finalised early"
        elif self._stop:
            self._state.status = RunStatus.STOPPED
            self._state.last_reason = "manually stopped"
        else:
            self._state.status = RunStatus.FINALIZED
            residual = max(0, self._state.q0 - self._state.units_sold)
            self._state.residual = residual
            self._state.last_reason = (
                f"closed at {self._state.current_rung} ₹{self._state.current_price:g}; "
                f"{residual} units residual write-off"
            )
        await self._event(run_id, self._state.status.value, self._state.last_reason)
        await self._sync(run_id)
        return self._state.last_reason

    async def _event(self, run_id: str, kind: str, message: str) -> None:
        await workflow.execute_activity(
            record_run_event, args=[run_id, kind, message], **_DB
        )

    async def _sync(self, run_id: str) -> None:
        await workflow.execute_activity(
            persist_state, args=[run_id, self._state], **_DB
        )

    # ------------------------------------------------------------- signals --- #
    @workflow.signal
    def owner_decision(self, decision: OwnerDecision) -> None:
        self._owner_decision = decision

    @workflow.signal
    def additional_grn(self, grn: AdditionalGrn) -> None:
        self._pending_grn += grn.qty

    @workflow.signal
    def sold_out(self) -> None:
        self._sold_out = True

    @workflow.signal
    def simulate(self, req: SimulateRequest) -> None:
        """Operator-driven sell-through for a sim-mode run. Absolute values;
        None fields keep the current value. Wakes the loop to re-decide now."""
        cur = self._sim or SimulateRequest()
        self._sim = SimulateRequest(
            units_sold=req.units_sold if req.units_sold is not None else cur.units_sold,
            recent_rate=req.recent_rate if req.recent_rate is not None else cur.recent_rate,
            q0=req.q0 if req.q0 is not None else cur.q0,
        )
        self._sim_dirty = True

    @workflow.signal
    def manual_override(self, ov: ManualOverride) -> None:
        if ov.action == "stop":
            self._stop = True
        elif ov.action == "force_rung" and ov.rung:
            self._force_rung_label = ov.rung

    @workflow.signal
    def set_standing_rule(self, req: StandingRuleRequest) -> None:
        if self._state is not None:
            self._state.standing_rule_pct = req.auto_approve_max_discount_pct

    # --------------------------------------------------------------- query --- #
    @workflow.query
    def current_state(self) -> MarkdownState | None:
        return self._state
