"""PerishableMarkdownWorkflow — the durable, day-long markdown agent (design §5–§8).

The workflow is pure orchestration: it sleeps on timers between checkpoints, runs
the deterministic decision engine in-process, waits on owner-decision signals, and
calls activities for every side effect. All timing comes from the pre-planned
checkpoint offsets so replay is identical.
"""
from __future__ import annotations

import asyncio
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
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
    from pricing.decision_engine import decide, price_for_rung
    from shared.models import (
        AdditionalGrn,
        Approval,
        AuditEvent,
        Decision,
        DecisionResult,
        ManualOverride,
        MarkdownState,
        OwnerDecision,
        RunStatus,
    )

# Activity option presets (design §7 retry/timeout matrix).
_READ = dict(
    # Generous start-to-close: fetch_sellthrough makes a live OUTWARDED count call
    # (~26-30s for slow movers) before falling back to the synthetic curve.
    start_to_close_timeout=timedelta(seconds=40),
    retry_policy=RetryPolicy(maximum_attempts=3),
)
_NOTIFY = dict(
    start_to_close_timeout=timedelta(seconds=30),
    retry_policy=RetryPolicy(maximum_attempts=5),
)
_APPLY = dict(
    start_to_close_timeout=timedelta(seconds=20),
    retry_policy=RetryPolicy(maximum_attempts=10),  # gate rung on confirmed write
)
_LLM = dict(
    start_to_close_timeout=timedelta(seconds=8),
    retry_policy=RetryPolicy(maximum_attempts=1),   # never block a markdown
)
_DB = dict(
    start_to_close_timeout=timedelta(seconds=15),
    retry_policy=RetryPolicy(maximum_attempts=5),
)


@workflow.defn
class PerishableMarkdownWorkflow:
    def __init__(self) -> None:
        self._state: MarkdownState | None = None
        self._owner_decision: OwnerDecision | None = None
        self._pending_grn: int = 0
        self._sold_out: bool = False
        self._stop: bool = False
        self._force_rung_label: str | None = None

    # ----------------------------------------------------------------- run --- #
    @workflow.run
    async def run(
        self,
        store_id: str,
        jpin: str,
        receipt_date: str,
        shadow_mode: bool = False,
        demo_speed: float = 1800.0,
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
        rungs = cfg.rungs
        total_h = float(cfg.store_close_hour - 8)  # T0_HOUR_IST in plan_run

        self._state = MarkdownState(
            store_id=store_id, jpin=jpin, receipt_date=receipt_date,
            clearance_date=receipt_date,  # L=1 in the pilot catalogue
            product_title=r.product_title, category=r.category, is_rte=r.is_rte,
            list_price=r.list_price, mrp=r.mrp,
            current_rung="R0", current_price=r.list_price,
            q0=r.q0, status=RunStatus.OBSERVING, shadow_mode=shadow_mode,
        )
        await self._sync(run_id)
        await self._event(
            run_id, "STARTED",
            f"{r.product_title} · Q0={r.q0} · list ₹{r.list_price:g} · "
            f"{'shadow' if shadow_mode else 'live'}",
        )

        start = workflow.now()
        current_rung_index = 0

        for cp in plan.checkpoints:
            await self._sleep_until(start, cp.sleep_offset_s)
            if self._stop:
                break
            if self._sold_out:
                break

            # Re-baseline on any additional GRN received since the last checkpoint.
            if self._pending_grn:
                self._state.q0 += self._pending_grn
                await self._event(run_id, "GRN",
                                  f"+{self._pending_grn} re-received; Q0={self._state.q0}")
                self._pending_grn = 0

            # Honour a manual forced rung before reading sell-through.
            forced_index = self._resolve_forced(rungs)

            current_pct = rungs[current_rung_index].ceiling_pct
            st = await workflow.execute_activity(
                fetch_sellthrough,
                args=[store_id, jpin, self._state.q0, cp.nominal_elapsed_h, total_h,
                      cfg.trailing_window_hours, current_pct],
                **_READ,
            )
            self._state.units_sold = st.units_sold
            self._state.run_rate = st.run_rate
            self._state.low_confidence = st.low_confidence

            past_gate = cp.wallclock_hour_ist >= cfg.rte_autoclear_gate_hour

            if forced_index is not None:
                result = self._forced_result(forced_index, rungs, r.list_price, cfg)
            else:
                result = decide(
                    q0=self._state.q0, units_sold=st.units_sold, run_rate=st.run_rate,
                    nominal_remaining_h=cp.nominal_remaining_h,
                    current_rung_index=current_rung_index,
                    ceiling_rung_index=cp.rung_index,
                    rungs=rungs, list_price=r.list_price,
                    token_free_price=cfg.token_free_price,
                    theta_hold=cfg.theta_hold, is_rte=r.is_rte, past_rte_gate=past_gate,
                )

            self._state.projected_clearance = result.projected_clearance
            self._state.residual = result.residual
            self._state.ratio = result.ratio
            self._state.last_reason = result.reason

            approval = Approval.NOT_REQUIRED
            from_rung = rungs[current_rung_index].label
            from_price = self._state.current_price
            apply_step = result.decision in (Decision.STEP, Decision.AUTO_CLEAR)

            if apply_step and result.requires_approval and not shadow_mode:
                approval = await self._seek_approval(
                    run_id, r.product_title, from_price, result.target_price,
                    rungs[result.target_rung_index].label, cfg, demo_speed,
                )
                if approval != Approval.APPROVED:
                    apply_step = False

            if shadow_mode:
                apply_step = False  # shadow: record recommendation, never apply

            if apply_step:
                current_rung_index = await self._apply(
                    run_id, store_id, jpin, result, rungs, from_price, cfg, shadow_mode
                )

            # Always log the *recommended* rung/price (the engine's decision),
            # even when shadow/reject/timeout means it wasn't applied.
            await self._record(
                run_id, rungs[result.target_rung_index].label, result, approval
            )
            await self._audit(run_id, from_rung, from_price, result, approval, rungs)
            self._state.status = RunStatus.OBSERVING
            await self._sync(run_id)

            if result.target_rung_index >= len(rungs) - 1 and apply_step:
                # Reached the token rung; nothing deeper to do.
                pass

        return await self._finalize(run_id)

    # ------------------------------------------------------------- helpers --- #
    async def _sleep_until(self, start, offset_s: float) -> None:
        elapsed = (workflow.now() - start).total_seconds()
        remaining = offset_s - elapsed
        if remaining <= 0:
            return
        try:
            await workflow.wait_condition(
                lambda: self._sold_out or self._stop,
                timeout=timedelta(seconds=remaining),
            )
        except asyncio.TimeoutError:
            pass

    def _resolve_forced(self, rungs) -> int | None:
        if self._force_rung_label is None:
            return None
        for r in rungs:
            if r.label == self._force_rung_label:
                self._force_rung_label = None
                return r.index
        self._force_rung_label = None
        return None

    def _forced_result(self, idx, rungs, list_price, cfg):
        price = price_for_rung(rungs[idx], list_price, cfg.token_free_price)
        return DecisionResult(
            target_rung_index=idx, decision=Decision.STEP,
            reason=f"manual override to {rungs[idx].label}",
            ratio=self._state.ratio, projected_clearance=self._state.projected_clearance,
            residual=self._state.residual, target_price=price, requires_approval=False,
        )

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

    async def _apply(self, run_id, store_id, jpin, result, rungs, from_price,
                     cfg, shadow_mode) -> int:
        self._state.status = RunStatus.APPLYING
        await self._sync(run_id)
        to_rung = rungs[result.target_rung_index]
        confirmed = await workflow.execute_activity(
            apply_price_goldeneye,
            args=[run_id, store_id, jpin, to_rung.label, from_price,
                  result.target_price],
            **_APPLY,
        )
        if not confirmed:
            await self._event(run_id, "APPLY_FAILED",
                              f"Golden Eye did not confirm {to_rung.label}")
            return rungs.index(next(r for r in rungs
                                    if r.label == self._state.current_rung))

        pct_off = round((1 - result.target_price / self._state.list_price) * 100, 1) \
            if self._state.list_price else 0.0
        headline = await workflow.execute_activity(
            shape_offer_llm,
            args=[self._state.product_title, pct_off, to_rung.token_free,
                  cfg.enable_llm],
            **_LLM,
        )
        await workflow.execute_activity(
            publish_offer,
            args=[run_id, store_id, jpin, headline, result.target_price,
                  to_rung.label],
            **_NOTIFY,
        )
        self._state.current_rung = to_rung.label
        self._state.current_price = result.target_price
        await self._event(
            run_id, "APPLIED",
            f"{to_rung.label} ₹{result.target_price:g} — {headline}",
        )
        if to_rung.token_free and result.decision == Decision.AUTO_CLEAR:
            await workflow.execute_activity(
                notify_owner,
                args=[store_id,
                      f"{self._state.product_title}: RTE auto-cleared to "
                      f"₹{result.target_price:g} at close."],
                **_NOTIFY,
            )
        return result.target_rung_index

    async def _record(self, run_id, rung_label, result, approval) -> None:
        await workflow.execute_activity(
            persist_decision,
            args=[run_id, {
                "rung": rung_label, "price": result.target_price,
                "units_sold": self._state.units_sold,
                "run_rate": self._state.run_rate,
                "projected_clearance": result.projected_clearance,
                "residual": result.residual, "ratio": result.ratio,
                "decision": result.decision.value, "approval": approval.value,
                "reason": result.reason,
            }],
            **_DB,
        )

    async def _audit(self, run_id, from_rung, from_price, result, approval, rungs):
        ev = AuditEvent(
            run_id=run_id, store_id=self._state.store_id, jpin=self._state.jpin,
            ts_ist=workflow.now().isoformat(),
            from_rung=from_rung, to_rung=rungs[result.target_rung_index].label,
            from_price=from_price, to_price=result.target_price,
            q0=self._state.q0, units_sold=self._state.units_sold,
            run_rate=self._state.run_rate,
            projected_clearance=result.projected_clearance, residual=result.residual,
            ratio=result.ratio, decision=result.decision, approval=approval,
            reason=result.reason,
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
    def manual_override(self, ov: ManualOverride) -> None:
        if ov.action == "stop":
            self._stop = True
        elif ov.action == "force_rung" and ov.rung:
            self._force_rung_label = ov.rung

    # --------------------------------------------------------------- query --- #
    @workflow.query
    def current_state(self) -> MarkdownState | None:
        return self._state
