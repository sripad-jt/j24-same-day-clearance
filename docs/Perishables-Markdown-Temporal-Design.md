# Temporal Workflow Design — Same-Day Perishables Clearance (Markdown Agent)

**Owner:** J24 Store Platform (Pillar 3) · **SDK:** Temporal Cloud, Python
**Status:** 🟡 Design — ready for review (approve_with_changes)
**Last updated:** 2026-06-18
**Source spec:** `docs/Agentic_Perishables_Markdown_Design.docx` (§10–§22)
**Data source:** `docs/inventory-item-details-api.md`

---

## 1. Summary

A durable, day-long Temporal workflow owns a single perishable batch for its life. It selects at-risk perishable SKUs each morning, observes sell-through through the day, runs a **deterministic** decision engine at each ladder checkpoint, asks the owner for consent when required (My J24), applies an approved price through **Golden Eye**, and hands the offer to retail media / the POS second screen. The LLM sits at the edges — copy and digest only, never the price.

The pricing arithmetic is deterministic; the *agentic* part is the orchestration — **observe → decide → ask → apply → learn** — which is exactly what Temporal is for. The team already runs Temporal for the FNV image workflow, so worker/deploy/on-call patterns are reused, not invented.

---

## 2. Why Temporal

A long-running, stateful process with durable timers, human-in-the-loop waits, and a hard audit requirement. The run sleeps on durable timers between checkpoints across a ~14-hour selling day and survives worker restarts. Owner approval and re-receipts arrive as **signals**. Deterministic **replay** gives the audit trail almost for free — the history *is* the record of why each price changed.

---

## 3. Temporal Cloud setup

| Concern | Decision |
|---|---|
| **Endpoint** | Namespace Endpoint `<namespace>.<account>.tmprl.cloud:7233` (e.g. `j24-perishables.<acct>.tmprl.cloud:7233`). No regional `*.api.temporal.io` endpoint — no HA-routing need in the pilot. |
| **Namespace** | Full Cloud format `<namespace>.<account-id>` (from `tcld namespace list`), region `ap-south-1` next to the S3 audit bucket and Golden Eye. |
| **Auth** | Match the FNV worker's method. API key → `TEMPORAL_API_KEY`; mTLS → client cert/key with CA uploaded via `tcld namespace accepted-client-ca add`. Default if fresh: **API key**. |
| **Worker connect (Python)** | `Client.connect()` from env: `TEMPORAL_ADDRESS`, `TEMPORAL_NAMESPACE`, plus `TEMPORAL_API_KEY` **or** `TEMPORAL_TLS_CLIENT_CERT_PATH` / `TEMPORAL_TLS_CLIENT_KEY_PATH`. |
| **Verify** | `temporal task-queue describe --task-queue j24-perishables --namespace <ns>` must show **pollers** before the worker is "healthy." "No pollers" = worker not connected, not a capacity issue. |
| **Rate limits** | Morning sweep starts one run per store×SKU×day → a burst of `StartWorkflow` = namespace write ops. Batch + backoff to avoid `RESOURCE_EXHAUSTED`. |

---

## 4. Candidate selection & data acquisition

Run by the morning **Schedule**, per facility, before any markdown run exists.

**Stage A — list the day's candidates.** Pilot starts narrow: **leafy greens, master shelf-life `L = 1` (must sell today).** Filter catalog/master by perishable category + shelf-life → a JPIN list for the facility. Widen to other F&B/RTE/FNV once L=1 is calibrated.

**Stage B — pull per-SKU facts** via the Inventory Item Details API (`POST …/space/product/details/for-state-status-facility`), called two ways:

| Field needed | Source | How |
|---|---|---|
| List of SKUs (leafy green, L=1) | Catalog / shelf-life master | Stage A → `jpins[]` |
| Master shelf-life / best-before days (L) | Catalog / batch master | `clearance_date = receipt + L − 1` |
| Listing selling price | Inventory API `listingSellingPrice` | Live from Lot Management per call (nullable → skip/flag) |
| List price / MRP | Catalog / Lot Management master | Anchor for markdown % (rungs apply against this) |
| Opening stock Q0 / current on-hand | Inventory API active states | `initialQty` (opening) + `leftQty` (on-hand) for `SELLABLE, FULFILMENT, INWARDED, UNDER_TRANSFER` + `ACTIVE/ONHOLD`; naturally bounded, no time window |
| Received date & qty | Inventory API `inventoryItemCreatedTime` + `initialQty`; origin via `originInventoryItemCreatedTime` | Establishes T0 / re-baseline on additional GRN |
| Mfr date / expiry date | Batch/lot master (joined to `lotId`) | Validates L; expiry = hard backstop for must-clear time |
| Sales / sell-through | Inventory API **`OUTWARDED`** | Sold units (`leftQty = 0`); **`createdTimeAfter` mandatory, ≥ now − 2 days** — pass clearance-day `T0`; cap with `maxResults`. Cross-check Golden Eye POS as the live feed. |

The run always passes its own same-day `T0`, so the 2-day `OUTWARDED` window constraint always holds. One Signal-With-Start fires per surviving candidate (`Q0 ≥ min-to-run`, non-null price).

---

## 5. Workflow model

```
Workflow ID:   perish-markdown-{store_id}-{jpin}-{receipt_date}   # ISO date
Reuse policy:  ALLOW_DUPLICATE          # one *running* run per batch; new run OK after prior closes
Start:         Signal-With-Start        # additionalGrn payload
Task queue:    j24-pilot-default (Phase 0 shadow) → j24-perishables (Phase 1+)
Worker:        Python (mirror FNV worker)
```

**Start / re-receipt.** First GRN, duplicate GRN, and genuine same-day re-receipt all issue the same `additionalGrn` **Signal-With-Start**: live run → re-baseline Q0 via the signal; no live run → start one. `ALLOW_DUPLICATE` (not the spec's `REJECT_DUPLICATE`) lets a legitimate post-completion re-receipt start a fresh run while a duplicate GRN still lands on the live run rather than starting a second.

### Lifecycle
1. **Start (first GRN):** `fetch_receipt_context` → L, Q0, category, RTE flag, list price/MRP, mfr/expiry. Compute `clearance_date` and `T0 = max(store_open, GRN/shelf-ready)`.
2. **Sleep to clearance day** (durable timer). On interim days (L≥2) wake once for the light **soft-nudge** check (cap 15%, normal approval).
3. **Walk the R0→R3 ladder** on the clearance day. Each checkpoint fires at `min(T0 + elapsed_offset, wall_clock_trigger)` — whichever first. `fetch_sellthrough` → pure decision engine → HOLD or propose a step.
4. **Approval wait:** if consent required, `request_owner_approval`, then `await ownerDecision` with a 30-min timer → timeout defaults to **HOLD** (non-RTE). RTE after the close gate (20:00 IST) → **auto-clear to ₹1**, notify after the fact.
5. **Apply & publish:** `apply_price_goldeneye` (advance rung only on confirmation) → optional `shape_offer_llm` → `publish_offer_retailmedia` → `write_audit`.
6. **Finalize at store close:** record residual write-off, emit metrics, complete.

### Markdown ladder (from spec §6, config-driven)
| Rung | Trigger (whichever first) | Ceiling | Behaviour |
|---|---|---|---|
| R0 | T0 (shelf-ready) | 0% | Observe; establish Q0 + baseline run-rate |
| R1 | T0 + 2h | 25% | First markdown — only if projected short at list |
| R2 | T0 + 8h, or 16:00 IST | 50% | Deep markdown |
| R3 | 21:00 IST | ₹1 | Clearance gesture for residual only; approval rules apply |

---

## 6. State, signals & queries

`MarkdownState`: identity (`store_id, jpin, receipt_date, clearance_date, category, is_rte`), pricing (`list_price, current_rung, current_price`), sell-through (`q0, units_sold, run_rate, projected_clearance, residual`), control (`awaiting_approval, shadow_mode, last_reason`), and `history[]`. Config (ladder, `theta_hold`, thresholds) is **snapshotted at run start**.

| Signal / query | Purpose |
|---|---|
| `ownerDecision(rung, approve\|reject)` | Unblocks the waiting step; ignored if not awaiting (idempotent) |
| `additionalGrn(qty, …)` | Re-baselines Q0; also the Signal-With-Start payload |
| `soldOut()` | Finalize early |
| `manualOverride(rung \| stop)` | Force a rung or halt |
| `currentState()` *(query)* | Read-only snapshot: rung, price, Q0, sold, projection, residual, last reason, awaiting-approval |

Coalesce signals via a signal-handler + `workflow.wait_condition` main-loop. Signal volume is tiny (one owner) — no flood risk.

---

## 7. Activities (retry/timeout matrix)

All I/O and non-determinism live in activities. Every activity is short-lived → no heartbeats.

| Activity | Start-to-close | Retry posture | Idempotency |
|---|---|---|---|
| `discover_perishable_skus` | ~20s | backoff | read-only (Stage A) |
| `fetch_receipt_context` | ~10s | backoff, modest attempts | read-only (Stage B active-state API) |
| `fetch_sellthrough` | ~10s | **few** attempts → fall back to all-day avg + low-confidence flag | read-only (`OUTWARDED` API, `createdTimeAfter=T0`) |
| `request_owner_approval` | ~30s | retry OK | key `{workflow_id}-{to_rung}` — no duplicate cards |
| `shape_offer_llm` | **~5–8s** | **≤1 attempt**, non-blocking | failure → deterministic copy; never blocks a markdown |
| `apply_price_goldeneye` | ~15s | backoff, **high** attempts until close; rung advances only on confirm; alert on repeated failure | key `{workflow_id}-{to_rung}` |
| `publish_offer_retailmedia` | ~15s | backoff | idempotent on offer id |
| `write_audit` | ~10s | backoff | S3 PUT at `{workflow_id}/{to_rung}/{ts}` |
| `notify_owner` | ~15s | retry | idempotent on event id |

---

## 8. Decision engine (determinism)

A **pure function inside the workflow**: given Q0, units sold, elapsed/remaining hours, current rung, checkpoint ceiling and config thresholds → target rung + one-line reason. No I/O, no randomness, all time via `workflow.now()` (never `datetime.now()`). Price is monotonic non-increasing within the day.

```
v        = units in trailing window / window_hours      # fallback: sold / elapsed
proj     = sold + v * hours_remaining_to_must_clear
ratio    = proj / Q0
if   ratio >= 1.0          -> HOLD                       # on track, no markdown
elif ratio >= theta_hold   -> step to min(current+1, Rc) # Rc = checkpoint ceiling
else                       -> step to Rc                 # lagging badly
theta_hold = 0.85 (config)
```

**Config is snapshotted at run start** (re-read at clearance-day start for L≥2) — never read live per-checkpoint (non-deterministic on replay; could mutate a rung mid-decision). Ladder re-tuning applies to *new* runs.

---

## 9. Cross-cutting design decisions

- **Continue-As-New:** not needed in v1. A run spans ≤ ~2 days but emits only low hundreds of history events — far under the 10k advisory / 50k hard limits. Trigger documented but won't fire.
- **Versioning:** runs live up to ~2 days, so a mid-day deploy must not break in-flight runs → **Worker Versioning (pinned Build IDs)** + `workflow.patched()` for logic changes.
- **Visibility:** custom Search Attributes (`store_id, jpin, category, current_rung, awaiting_approval, shadow_mode`) for **filtering only**, not as system of record. State of record = workflow + immutable S3 audit log.
- **Starter:** a Temporal **Schedule** runs the §4 funnel and Signal-With-Starts surviving candidates (batched + backoff). Timers handle intra-day delays. (Schedule for recurring launch, timers for relative delays — not cron.)
- **Task queue:** `j24-pilot-default` for Phase 0 shadow (~4 stores); dedicated `j24-perishables` from Phase 1 for concurrency/rate isolation from FNV.

---

## 10. Audit event

Every applied change emits an immutable record (to S3 via `write_audit`):

```
AuditEvent { workflow_id, store_id, jpin, ts_ist,
  from_rung -> to_rung, from_price -> to_price,
  q0, units_sold, run_rate, projected_clearance, residual, ratio,
  decision: HOLD | STEP | AUTO_CLEAR,
  approval: APPROVED | REJECTED | NOT_REQUIRED | TIMEOUT_HOLD,
  reason: str }
```

The operator's key view is the per-run reason trail — "held at list → 25% because projected short by 8 → ₹1 auto-clear at close."

---

## 11. Recommended defaults (open questions, spec §22)

| Question | Default (confirm before Phase 1) |
|---|---|
| T0 anchor | `max(store_open, GRN-complete)` |
| Evening floor | Per-store close, default 21:00 IST; RTE auto-clear gate 20:00 IST |
| Approval timeout | 30 min → default HOLD (non-RTE); no auto-escalation in v1; track latency |
| Soft-nudge cadence (L≥2) | Once per interim day, cap 15% |
| Task queue | `j24-pilot-default` (Phase 0) → `j24-perishables` (Phase 1+) |
| Offer publisher / cohort signal | Out of Temporal scope — product decision, left open |

---

## 12. Design critique (vs `temporal-design` rubric)

**Verdict: `approve_with_changes`.** Excellent Temporal fit; non-determinism isolated to activities, LLM off the price path. The changes below harden the spec's §10–§22 for production.

**Top issues:** (1) **high** — `REJECT_DUPLICATE` drops legitimate post-completion re-receipts → Signal-With-Start + `ALLOW_DUPLICATE` (§5). (2) **high** — config read strategy unspecified → snapshot at run start (§8). (3) **medium** — uniform/implicit activity policies → per-activity matrix + idempotency keys (§7). (4) **medium** — no versioning story for ≤2-day runs → Worker Versioning + patching (§9). (5) **medium** — determinism hygiene: `workflow.now()` only, no `random`/`uuid` (§8). (6) **low** — search attributes for visibility only (§9). (7) **low** — morning-sweep start burst vs write rate limits → batch + backoff (§3).

**Checklist:** determinism, retries/timeouts, idempotency = pass-with-changes; CAN = pass (not needed); signal volume = pass; versioning = fixed; visibility = pass; Cloud connectivity = inconclusive until FNV auth method confirmed.

---

## 13. Verification

1. **Cloud:** `tcld namespace list` shows the namespace; `temporal task-queue describe` shows pollers.
2. **Candidate funnel:** morning Schedule against a pilot facility — Stage A returns leafy-green L=1 JPINs, Stage B populates Q0/on-hand/received-time/price/expiry, `OUTWARDED` call (with `createdTimeAfter=T0`) returns sales without a 400, one run Signal-With-Started per surviving candidate.
3. **Shadow mode (Phase 0):** `shadow_mode=true` — logs recommendations, no price writes/cards; reason trail reads in plain language in the UI + S3.
4. **Determinism/replay:** replay a captured history against the worker — zero non-determinism errors (this is also the audit guarantee).
5. **Signals:** exercise `ownerDecision` approve/reject, 30-min timeout→HOLD, `additionalGrn` re-baseline, `soldOut`, `manualOverride(stop)`.
6. **Idempotency:** duplicate GRN + retried `apply_price_goldeneye` → single card, single price write, one audit record per rung.
7. **LLM kill-switch:** force `shape_offer_llm` timeout → deterministic copy used, markdown not delayed.
8. **Failure modes:** stale POS → all-day-average fallback + low-confidence; Golden Eye write fails → rung does not advance until confirmed.
