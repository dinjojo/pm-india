# Goal planner: parity with the Freefincal workbook

Status after the 20 Oct 2026 audit-and-fix pass. The planner in `core/goals.py`
implements the Freefincal robo-advisor workbook (July 2025 xlsm). This document
is the map for future agents: what matches at the formula level, what was fixed,
what intentionally differs, and where the evidence lives. Read
`docs/xlsm-audit-notes.md` for the raw extraction. The governing rule: match the
workbook's *logic*; parameters come from the app's `Assumptions`, never from the
workbook's stored sample inputs.

## What matches (formula-level, verified)

| Logic | Workbook | App |
|---|---|---|
| Goal cost at goal date | `Step 4A!B9 = B6·(1+infl)^years` | `fv(cost_today, …)` |
| Existing money grows only during the SIP window | `B19` | `_fv_existing` |
| Net corpus `B18 = B9−B16−B19` (app floors at 0) | `Step 4A!B18` | `one_time_goal` |
| One-time glide ends + 3 segments (50%/40% hold, 30% mid, rest end) | `Settings!E45/G45/C46-48` | `glide_path`, `_glide_weights` |
| Per-year blended returns `F·C40 + G·C41` | `Step 4A!H3` | `one_time_goal` |
| Retirement planning period incl. spouse gap | `Settings!C10` | `planning_period` |
| Bucket tenures `MIN(C10,15)`, 50/25% or 25/20%, remainder | `lsb E13:E16` | `bucket_ladder` |
| Bucket equity mixes 20/30/70, 40/50/100, 50/70/100% | `Settings!C20:C22` | `bucket_ladder` |
| Bucket corpus: PV at 5% inside each bucket, then each bucket's own glide-average rate until it starts (`R6 = R5/(1+r)^MAX(K)`) | `bb M6/R6/W6/AB6` | `bucket_corpus` |
| Bucket returns: glide-path geometric averages, rounded to 3 | `lsb F17:F19` | `bucket_glide_return` |
| First-year expense; inflation switches at retirement (C3 → C5) | `lsb F6` | `retirement_plan` |
| Income streams: grow to retirement, pay from start year, escalate yearly after | `Step 2!C32`, `Step 6B AE` | `retirement_plan` |
| Corpus in hand: equity at C7, taxable FI at C8−1%, tax-free at 7% | `Step 2!C14:C18` | `retirement_plan` |
| Income floor: FLAT nominal shortfall × B9, priced at the 4% annuity rate (`F12 = L51/H7`); netted flat against every year's need | `lsb L51`, `Settings!H7` | `retirement_plan` |
| Emergency corpus: 5% × max(investments in hand, bucket corpus) | `lsb F13` | `retirement_plan` |
| Recurring goals: a separate sinking fund per payment, `G = F·(r−g)/(12·((1+r)^n−(1+g)^n))`, r = C37, g = the SIP step-up, funds start when the previous payment is made | `Step 4B!G` | `recurring_goal` |
| SIP accumulation: ANNUAL schedule `(12·M3+N2)·(1+L3)`, step-up yearly | `Step 3!N`, `Step 4A!J` | `solve_sip` |
| Pre-retirement glide: hold 60% equity, two-stage decline to 20% (retire ≥60) or the ladder's weighted equity share; <10 years starts at the end weight; FI at C8 (full 7%) | `Step 3!H`, `Settings!E25/G25/C26-28` | `retirement_plan`, exposed as `pre_ret_glide` |

On the workbook's own sample case (age 35, spouse 30, retire 50, ₹50k/month,
5% step-up) the app now reproduces its corpus and SIP exactly (₹4.91 Cr,
₹1,00,287/month) — a consequence of the logic matching, not a tuned target.

## Fixed on 20 Oct 2026 (were deviations, now workbook-faithful)

1. Bucket returns were a flat blend (8.5/9.1/10%) → glide-path averages (7.8/8.6/9.3% with defaults).
2. Bucket corpus was sized by a refill-and-rebalance simulation the workbook doesn't have → the workbook's PV sum. The simulation survives as a display-only cash-flow table; it never sizes the corpus or SIP.
3. Emergency corpus was 6 months of expenses → 5% of the larger of investments-in-hand and bucket corpus.
4. Income floor was an inflation-indexed annuity at 6% → flat nominal shortfall at the 4% annuity rate.
5. SIP compounding was monthly at r/12 → the workbook's annual schedule. (The SIP/lumpsum projection calculator in `fund_analytics.py` is a different feature and keeps monthly.)
6. Recurring goals pre-funded one annuity-due at 6% → per-payment sinking funds at C37 with step-up, investing continuing through the goal period; result now carries per-payment `occurrences`/schedule.
7. Pre-retirement allocation was a flat 65%/8.6% → the Step 3 glide.
8. `spouse_age` was stored but unreachable from the UI → optional whole-number field in goal settings (`SETTINGS_FIELDS`), blank = single.
9. Screener thresholds defaulted to 0.6/none/none → 0.7 for every screen (workbook defaults); a blank query param now means "no filter".

## Workbook bugs we deliberately do NOT copy

- `low stress!F19` reads B29 (the medium-risk toggle) instead of B30 — the app always uses the glide average.
- `Settings!E146` starts the low bucket's glide at 40%, which can exceed the bucket's own 20% mix — the app caps the band at the bucket mix.
- The AO/AT income columns use `k2b` where `k3b`/`k4b` were meant.

## Intentional app-only additions (no workbook basis; kept)

Bear/Base/Bull scenarios and the De-risk/Retire+3 variants; the drawdown
cash-flow display and depletion ages; SIP affordability bands (25/40% of
take-home); realism warnings in `assumption_notes`; a per-plan inflation
override; `goal_progress` (current SIP from recent purchases). These are
features, not deviations of the ported math.

## Still different / not ported (tracked in docs/pending-work.md)

Under-26 advice follows the app's shape, not the workbook's Step 1 (150% of
EPF contribution rule). Workbook-only features remain unported: goals that
continue past retirement add to retirement need; the already-retired 15-year
test and annuity rule; reduced returns approaching retirement; custom
allocation schedules; the unified-portfolio module (Step 6A/6B); salary
growth + EMI cash-flow check; annuity laddering; recurring-goal intervals
longer than a year; maturity-value subtraction from corpus.

## Screener notes

The workbook's Results/Excess sheets hold pasted values; the live logic is its
formulas. Score denominator is `outperformed / MIN(index_n, fund_n)` in all
1,647 window cells (an earlier read claiming 8 artifact rows was wrong —
corrected in `core/screener.py` and `docs/mf-forecasting-research.md`). The
workbook rolls daily (up to ~3,515 one-year windows per fund); the app rolls
monthly by design, so per-fund window counts are far smaller.
