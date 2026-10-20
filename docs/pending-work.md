# Pending work and known gaps

A running note of everything that is deliberately not done, waiting, or known
to differ. Last updated: 20 Oct 2026.

## Goal planner (differences from the Freefincal workbook)

The 19 Oct audit's deviations 1–4 and 8 below were fixed on 20 Oct 2026: the
planner now follows the workbook's logic (sinking-fund recurring goals, flat
annuity floor at 4%, PV bucket corpus at 7.8/8.6/9.3% glide-average returns
with the 5% emergency rule, annual SIP schedule, editable spouse_age). See
`docs/goal-planner-parity.md` for the full map. What remains deliberate:

1. **Recurring-goal intervals.** The workbook allows payments every N years;
   the app's recurring goals are yearly. The sinking-fund math supports any
   interval (`recurring_goal(every_years=…)`), but the form/summary rows don't
   expose it yet.
2. **Young earner advice.** The app's under-26 advice (emergency fund, 100%
   equity, 10% step-up) is not the workbook's version (150% of EPF/NPS
   contribution in equity, 20 to 25% of gross income, FIRE option, review at
   30). Deliberate: the app's path is more actionable.
3. **Affordability.** The app uses static bands (25/40% of take-home). The
   workbook checks a year-by-year cash-flow rule (salary growth, expenses,
   EMIs, all goal SIPs) and flags red years.
4. **Workbook features not ported at all:** the unified portfolio GoalSeek
   (Step 6B), the annuity laddering sheet (slab tax and 18% GST), DIY
   buckets, custom allocation schedules, the "lower returns after
   retirement" option, the already-retired annuity rule, the
   goal-after-retirement warning, and adding goal SIPs that continue past
   retirement to the retirement expense.

## Tax

1. **Tax lots ignore the user and the account.** Realized gains and
   unrealized lots are matched FIFO per asset across all accounts, and
   without a user filter. A second user on the same instance could have
   another user's buys paired with their sells.
2. **Open lots collapse purchases by oldest date**, and manual holding values
   carry no cost basis, so reported gains can be off for imported snapshots.
3. **Duplicate rows in the income and deductions section** (PPF x5, EPF x4,
   NPS x4). The view builds the scheme list wrong; the template is fine.
4. **Harvest callout vs headroom bar.** The callout always uses the current
   financial year; the exemption bar uses the selected year. The main
   estimate also does not let short-term losses offset long-term gains; the
   callout does, so the two "tax now" numbers can differ.

## Screener

1. **Upside/downside consistency definition.** The workbook's exact formula
   could not be recovered from the file (it looks like a capture-ratio test).
   The app's version (beats the benchmark in up/down benchmark windows) is
   reasonable but not proven identical.
2. **Rolling windows.** The workbook counts daily rolling windows; the app
   counts monthly ones.
3. **smallcap250_tri is now unused** by any category after the benchmark
   alignment. Keep or drop the CSV.
4. **Crisil 65:35 proxy** uses a liquid fund as its debt leg, not a composite
   bond index.
5. **Filters cannot stack across windows** the way the workbook's
   AutoFilters can (for example 3Y and 5Y at once).
6. **The refresh cron job has not run yet.** First run is 07:00 tomorrow;
   check `data/refresh.log` after.

## Ops

1. **The Docker image on :8010 is old.** Rebuild with
   `docker compose build && docker compose up -d` to include everything up
   to commit 04cbdf5.
2. **No systemd service.** The dev server on :8020 survives terminal close
   (setsid) but not a reboot. A `pm-india.service` unit is the clean fix if
   wanted.
3. **Email auto-import is recommended but not built.** Preferred design: IMAP
   with an app password watching a label; a command pulls CAS mails from
   CAMS/KFin, broker notes and premium reminders, and feeds the existing
   importers. Full Gmail OAuth access was rejected as too broad for a local
   app.
4. **No EPF passbook importer.** EPF enters via the photo scan or manual
   entry only. A real importer would parse an EPFO passbook PDF into
   amount-only transactions.
5. **Broker re-import risk.** Four broker rows in the dev database were saved
   under the old duplicate check. Importing that same file once more may
   duplicate them; the current code checks duplicates correctly.
6. **PPF group trend shows about -54%.** The value snapshot history for PPF
   is partly backfilled estimates. The chart is labelled but looks wrong.

## UI

1. **Dashboard "Needs attention" card spacing.** The goal note line runs
   close to the Off track badge. Cosmetic.
2. **"Invest once, today" wording on one-time goals** is now slightly off:
   the lump sum only earns growth from the SIP start year, matching the
   workbook, but the label still says "today".
3. **Update value row shows a dash for Weight** because the view only passes
   the holding, not the portfolio total.
4. **No stock analytics pages.** Stocks link out to Yahoo Finance only.
   Internal stock views (price history, dividend record) were discussed and
   parked.
5. **Long scheme names wrap to two lines** in the Funds table on some rows.
