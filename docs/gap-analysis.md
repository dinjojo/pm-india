# Gap analysis: PM India vs Kuvera, INDmoney, Tickertape and freefincal

PM India is a single-user, self-hosted Django app. Data stays in a local SQLite
database, and the only network calls fetch public price and NAV data. This
document compares it with four Indian products on seven capability areas. For
each gap it says what the other products do, what this app does today (checked
against the code, not the README), how well the gap fits the local-first
design, and whether to build it, skip it, or leave it for later.

- **Sources checked on:** 19 October 2026. All competitor claims come from the
  public pages listed in [Sources](#sources). I could not inspect any
  competitor's logged-in product, so where a page does not show whether a
  feature is free or paid, still shipping, or how deep it goes, this document
  says so.
- **App state checked against:** `core/` and `templates/` at the current
  working tree.
- **Protection in this change:** policies are recorded manually, with annual
  premiums, renewal dates and a configurable 10–15× income term-cover check.
  Overdue term renewals are not counted toward cover. It **does not send
  notifications**. Like goal assumptions, its settings are installation-wide.

## Summary

| Area | Kuvera | INDmoney | Tickertape | freefincal (robo tool / NaviPlan) | PM India today | Recommendation |
|---|---|---|---|---|---|---|
| Account aggregation | MF consolidation via CAS forwarding | Bank, demat, MF, EPF, NPS, cards, loans through depositories, account aggregators (AA) and MF Central | Broker sync for about 11 brokers | NaviPlan: manual entry, CAS and CSV import | Manual uploads: CAS PDF, 4 broker CSVs, 3 bank CSVs, OCR, manual entry | **Skip** AA. **Build** a better import pipeline |
| Email auto-import (CAS, premiums) | Forward CAS mail to `import@kuvera.in` | Gmail OAuth read access (investments, insurance, card statements) | Not found | Not found | None | **Later**: a local drop folder first, then optional IMAP. **Skip** cloud OAuth |
| Stock analytics | No comparable research suite verified | Indian/US stock portfolio analytics | Full: screener (200+ filters), scorecards, forecasts, red flags, many of them Pro-only | Monthly stock screener posts (not a tool) | Yahoo quote links, but no automated stock price feed or in-app fundamentals; manual values or cost fallback | **Build** a stock price refresh. **Skip** fundamentals and forecasts |
| Tax-loss harvesting | Recommends; user places the order | Tax report only (no harvesting tool found) | Not found | NaviPlan: FIFO, folio-level unit recommendations, export | Finds losses and gain headroom per FY, with no unit list and known accuracy bugs | **Build** accurate identification. **Skip** order execution |
| Goal tracking depth | Goal plans | Map investments to goals, required vs actual SIP | None | Robo tool: deepest planning engine. Review tool: expected vs actual. NaviPlan: tag holdings to goals | Deep planning (workbook port). Progress uses whole-portfolio inputs; holdings are not mapped to goals | **Build** holding-to-goal mapping and an expected-vs-actual path |
| Family / shared portfolios | Family and joint accounts under one login; Managed Account with OTP approval | Invite members, consent-based shared view, revocable | Link multiple accounts, switch portfolios (app release note) | NaviPlan: labelled PRANs, health policy members | `Account.owner_label` free-text tag and an "Owner" grouping. **Not shared access** | **Build** per-owner (per-PAN) tax maths. **Skip** multi-login sharing |
| Alerts and renewal reminders | Email notifications (generic) | Card due-date reminders, pending bills | Basic alerts, plus Pro alerts | NaviPlan: premium-due dashboard, mark-as-paid, snooze | In-app dashboard notices and Protection renewal dates, no notifications | **Build** in-app due list and `.ics` export. **Later**: optional local email |

## What PM India already does (baseline)

The gap sections below rely on these facts from the code:

- **Imports** (`core/importers/`)
  - CAMS/KFintech CAS PDF, with transaction history, via `casparser`.
  - NSDL/CDSL demat CAS.
  - Broker holdings CSV: Zerodha, Groww, HDFC, Kotak, or a generic mapping.
  - Bank statement CSV: HDFC, ICICI, Yes Bank, or generic.
  - OCR of a photographed slip, and manual entry.
  - All parsing runs locally. Re-importing the same file is idempotent.
- **Prices** (`core/management/commands/update_prices.py`)
  - Only mutual fund NAVs refresh, from AMFI `NAVAll.txt`. The docstring says
    stock and crypto feeds are "pluggable", but none exists.
  - Without a `PricePoint`, a holding shows at cost or at a manually entered
    value.
  - `value_gaps()` flags missing and stale prices on the dashboard.
- **Fund analytics and screener** (`fund_analytics.py`, `screener.py`,
  `portfolio_tools.py`)
  - Mutual funds only: risk and return metrics, DMA signals, Monte Carlo
    VaR, CAPM against NIFTY 500 TRI, rolling-return consistency, MPT, and an
    AMC-name overlap check. Stock-level overlap is a TODO.
- **Tax** (`core/tax/gains.py`, `core/report_grouping.py`)
  - FIFO realised gains per FY under the post-July-2024 rates, tracking of
    the ₹1.25L exemption, and unrealised gains by category.
  - "What if I harvest now": losses available, the tax saving from offsetting
    them, carry-forward, and long-term gains bookable tax-free.
  - The dashboard shows lots turning long-term within 60 days
    (`tax_events`).
- **Goals** (`core/goals.py`, `GoalPlan`, `RetirementSettings`)
  - A port of the freefincal robo-advisor workbook: one-time, recurring and
    retirement goals, a 4-bucket drawdown, income streams with start and stop
    years, an annuity floor, scenarios, and year-by-year cash flow.
  - `goal_progress()` answers "on track?" with required vs current SIP.
- **Family**
  - `Account.owner_label` is a free-text label. Holdings can be grouped by
    it. Nothing else reads it.
- **Users**
  - Django auth with a signup view exists. Some data is global, not
    per-user: `RetirementSettings` is a singleton, `Asset` and `PricePoint`
    rows are shared, and `unrealized_lots()` reads every user's
    transactions. The app is effectively single-user and has no sharing or
    permission model.

---

## 1. Account aggregation

**What others do**

- **INDmoney** syncs bank accounts, demat holdings across brokers, MF folios,
  EPF, NPS, credit cards and loans. Its net-worth page says data is synced
  "through depositories and account aggregators". External mutual funds come
  in via an MF Central OTP consent flow.
- **Tickertape** fetches stock portfolios from about 11 broker partners
  (Zerodha, Upstox, Groww, Angel One and others). The pricing table lists
  "Link multiple demat accounts". From the public page I could not tell
  whether that is free or Pro-only.
- **Kuvera** consolidates external mutual funds through CAS import (see §2).
  Broader bank or demat aggregation was not verified in the reviewed sources.
- **freefincal's NaviPlan** is a paid (₹2,500/year), browser-local tracker. It
  imports CAS and CSV files, including NPS CRA statements, and accepts manual
  entry. It does not aggregate accounts.

**Current state**

- Every source is a manual upload: CAS PDF, 4 broker CSVs, 3 bank CSVs, OCR,
  or manual entry. There are no NPS or EPF file importers. EPF goes in through
  the photo-scan OCR path (`core/ocr.py`, covered by
  `tests_importers_epf.py`) or the manual add form. PPF and NPS are
  balance-type holdings with a manual `current_value`.

**Fit with local-first**

- **Poor for AA.** Under the RBI NBFC-AA Master Direction, a Financial
  Information User must be "an entity registered with and regulated by any
  financial sector regulator". Sahamati's onboarding steps start with
  registration with RBI, SEBI, IRDAI or PFRDA. A self-hosted personal app
  cannot become an FIU. Using a third party's FIU would route data through
  their servers, which defeats the purpose.
- **Direct broker APIs** (for example Kite Connect) are per-user and could
  run locally. They still need the user's API keys, and some are paid. I did
  not verify current terms.

**Recommendation**

- **Skip** AA and cloud aggregation. **Build** better local import coverage
  instead:
  1. An NPS CRA statement parser, so NPS stops being a manual balance.
  2. An EPF passbook PDF parser, to complement today's OCR path.
  3. Demat CAS imports that keep transaction dates, where the statement has
     them.
- These close most of the practical gap. Each one is a file the user already
  receives.

## 2. Email / Gmail auto-import of CAS and premium reminders

**What others do**

- **Kuvera** asks the user to add a mailbox rule that forwards CAMS/KFintech
  CAS emails to `import@kuvera.in`. Imports then happen automatically each
  time the user triggers a CAS.
- **INDmoney**'s privacy policy describes optional Gmail OAuth consent. It
  gives read-only access to "financial services-related emails including
  investment details, income tax documents, credit card statements, expenses,
  insurance policies, bill payments". INDmoney says it undergoes Google's
  annual third-party security assessment. A March 2026 r/personalfinanceindia
  thread shows users routing statements to a separate inbox to limit that
  access. This is anecdotal, but it shows privacy-minded users push back.
- **INDmoney premium reminders:** the net-worth page lists "insurance
  premiums" under *Pending Bills* liabilities. I found no official page that
  confirms automatic premium-due reminders parsed from email. Treat this as
  unverified.
- **Tickertape, freefincal:** no email import found.

**Current state**

- None. CAS import is a manual PDF upload with a password field. The CAS
  importer writes uploads to `/tmp/<user_id>_cas.pdf` before parsing.

**Fit with local-first**

- **Mixed.** Forwarding mail to a vendor, or granting a hosted service Gmail
  OAuth, both send statements off the machine. That breaks the README's "your
  financial data never leaves your machine".
- **Local options do fit:**
  - **Watched drop folder.** The user saves CAS attachments to a folder and a
    management command imports anything new. No credentials are needed. This
    is the pattern the Reddit poster describes.
  - **IMAP pull from a dedicated statements mailbox,** run on the user's
    machine. Gmail needs IMAP enabled and an app password, or OAuth.
    `gmail.readonly` is a *restricted* scope. Google exempts personal-use
    apps (under 100 users, or in Testing status) from verification, but
    testing-mode refresh tokens expire. Every self-hoster would have to
    create their own Google Cloud project. That is fragile, so prefer IMAP or
    the drop folder.
- **Premium reminders parsed from insurer emails** are a poor fit. Insurer
  email formats vary, and the Protection page already stores the
  renewal date, which is the reliable source.

**Recommendation**

- **Later.** First add a drop-folder import (`manage.py import_inbox
  <dir>`). It reuses `import_cas_pdf` and keeps the PAN password in local
  settings or an env var.
- After that, optional IMAP fetch for a user-designated mailbox, limited to
  CAS senders.
- **Skip** hosted forwarding addresses, and skip parsing insurer emails for
  premiums.

## 3. Stock analytics

**What others do**

- **Tickertape** is the reference here:
  - Stock screener with 200+ filters across 4,600+ Indian stocks, plus custom
    screens.
  - Performance, valuation and profitability scorecards, and red flags.
  - Analyst ratings and price, revenue and EPS forecasts.
  - Portfolio diversification score, and forecast and performance tiers.
- **Tickertape Pro** costs ₹399/month, ₹899 for 3 months, or ₹2,999/year (as
  of 19 Oct 2026; the FAQ mentions a time-limited 20% annual discount).
  Forecasts, advanced screens, custom filters, many prebuilt screens, data
  export and "Pro alerts" are in the Pro column. The extracted pricing table
  did not show free-vs-Pro ticks for every row, so treat individual feature
  placement as uncertain.
- **Tickertape's data source:** its analytical-tools disclosure says the data
  comes from "authorized exchange-approved data vendors", including S&P
  services, and that S&P data may not be used to train AI. That is licensed
  data.
- **INDmoney** advertises Indian stock analytics across brokers.
- **freefincal** publishes monthly stock screener posts (articles, not an
  interactive tool).

**Current state**

- No in-app stock analytics. Stock and ETF names link out to Yahoo Finance;
  mutual fund names with known scheme codes link to internal fund details.
- There is no stock price feed, so CSV- or CAS-imported stocks are valued at
  cost unless the user types a value. The dashboard flags these as "missing".
- `fund_analytics.py` and `screener.py` work on AMFI scheme codes only.
- Stock-level fund overlap is an explicit TODO in `portfolio_tools.py`. It
  needs portfolio disclosure data.

**Fit with local-first**

- **Prices: good.** End-of-day prices are public. `jugaad-data` is already
  a dependency, used for the benchmark, and can fetch NSE bhavcopy locally.
  Without prices, net worth, XIRR, unrealised gains and harvesting are all
  wrong for stock holders. This is a correctness gap, not a feature gap.
- **Fundamentals, forecasts and scorecards: poor.** They need licensed
  vendor data and constant upkeep. That conflicts with a free, offline,
  self-hosted tool, and it duplicates what Tickertape and Screener.in do
  well.

**Recommendation**

- **Build** a daily `update_prices` step for `Asset.AssetType.STOCK` (and
  ETFs, SGBs) from NSE/BSE bhavcopy, keyed by ISIN.
- Optionally reuse the existing return and risk metrics on stock price
  history.
- **Skip** fundamental screeners, analyst forecasts and scorecards. Link out
  to external sites if needed.
- **Later:** stock-level MF overlap, if a free and redistributable source of
  monthly AMC portfolio disclosures is confirmed.

## 4. Automated tax-loss harvesting: identification vs execution

Harvesting has two separate parts:

- **Identification:** which units to sell, how much, and the tax effect.
- **Execution:** placing the redeem or sell order and the rebuy.

The competitors split cleanly along that line.

**What others do**

- **Kuvera:** "We monitor your portfolio, and recommend a transaction when
  applicable… **You initiate the transaction**." It covers LTCG gain
  harvesting up to ₹1.25L on equity funds, keeps a per-FY history of tax
  harvested, and calculates harvested tax automatically on each sell.
  - Identification is automated; execution is user-initiated.
  - Funds have to be on Kuvera for one-tap action.
  - I found no claim of fully automatic order placement.
- **NaviPlan (freefincal):** Tax Center in v1.6.0 (October 2026), ₹2,500/year,
  runs locally.
  - FIFO simulation per folio. Recommends specific units to sell, either to
    harvest losses or to book LTCG close to (not over) the remaining ₹1.25L.
  - Nets gains and losses per redemption the way CAMS/KFintech capital-gains
    statements do. Exports to PDF or Excel.
  - The user selects which folios belong to one individual, because the
    exemption is per person.
  - No execution.
- **INDmoney:** Tax Centre is a consolidated tax report mapped to ITR
  schedules for holdings on INDmoney. Its blog explains harvesting, but I
  found no official harvesting-recommendation feature. Treat this as unknown,
  not absent.
- **Tickertape:** no harvesting feature found.

**Current state: identification only, at an aggregate level, with accuracy
problems**

- **What exists:**
  - `harvest_now()` totals short- and long-term unrealised losses, excluding
    crypto, and recomputes this FY's tax after set-off.
  - It shows carry-forward and "₹X of LT gains bookable tax-free".
  - The template lists losing assets by name.
- **No unit- or lot-level list.** The app never says "sell N units of
  folio F". `unrealized_lots()` merges every remaining buy of an asset into
  one `UnrealizedLot`, dated at the *earliest* remaining buy. So:
  - An asset bought both more and less than 12 months ago is classified
    entirely by its oldest lot.
  - Short- and long-term splits, `is_long_term`, and the 60-day "turns
    long-term" notices can be wrong for SIP investors. That is most users.
- **Lots are pooled across accounts and owners.** FIFO runs per asset, not
  per asset per account. `harvest_now()` ignores `owner_label`, so the ₹1.25L
  headroom is computed for the combined pool. But the exemption applies per
  individual. `unrealized_lots()` also reads all users' transactions;
  per-user filtering happens only later, for starring.
- **Cost basis is wrong for snapshot imports:**
  - Broker CSV rows without a date are booked as a BUY dated *today*.
  - NSDL/CDSL demat equities are booked at the statement's *market price*
    on the statement date.
  - In both cases holding period and cost are wrong, so gains and harvesting
    advice for those positions are unreliable.
- **No automatic stock prices** (§3). Without an imported or manually entered
  `PricePoint`, tax lots are valued at cost and cannot appear as losses. A
  holding-level manual value does not flow into these lot valuations.
- **Missing costs:** exit loads, STT, and the 12-month clock reset on rebuy
  are noted in the UI text but not modelled per lot. Grandfathering for
  equity bought before 31-01-2018 is listed as a planned addition in
  `gains.py`.
- **Wash sales:** the template says India has no wash-sale rule, which
  matches mainstream guidance. Some sources raise GAAR scrutiny of purely
  tax-motivated round trips. That is a caveat, not a verified constraint.

**Fit with local-first**

- **Identification: excellent.** It is pure computation on local data.
  NaviPlan shows a local-first product can do it well.
- **Execution: poor.** It needs broker or RTA order APIs, stored credentials,
  regulatory exposure (order placement on someone's behalf), and a live
  connection.

**Recommendation**

- **Build accurate identification**, in this order:
  1. Keep FIFO lots per buy and per account, instead of collapsing them per
     asset.
  2. Compute exemption headroom per owner or PAN (see §6).
  3. Flag positions whose cost or date came from a snapshot import as
     "basis unknown". Exclude them from recommendations until the user
     confirms them.
  4. Output a unit-level plan: folio or account, units, gain, and whether
     each is short- or long-term. Optimise LTCG to land just under the
     remaining exemption. Net per redemption the way the CAMS capital-gains
     statement does.
  5. Export to CSV.
- **Skip** order execution, permanently. The user places orders on their own
  platform.

## 5. Goal tracking depth

**What others do**

- **freefincal robo-advisor tool** (paid spreadsheet; the FAQ lists ₹5,625
  for the Google Sheets edition, with discount codes):
  - Retirement plus 6 one-time and 4 recurring goals, bucket strategy, and
    cash flow.
  - Custom asset-allocation schedule (October 2024 update).
  - Equity glide paths for retirement buckets (July 2025 update).
  - Coast-FIRE module.
- **freefincal review tool:** the Goal-based Portfolio Review/Audit Tool
  (updated September 2026) compares expected corpus growth against actual
  values entered each year. The FAQ says planning, tracking and review need
  all three tools.
- **NaviPlan:** tags holdings (MF, stocks, FD, NPS, insurance maturities) to
  goals and projects corpus.
- **INDmoney:** "map your investments" to a goal, then see progress %,
  required vs actual SIP and the remaining gap. It warns that mapping the same
  investment to two goals double counts.
- **Kuvera:** offers goal-based investment plans. The public pages don't show
  how deep its tracking goes, so I could not assess it.
- **Tickertape:** no goal feature found.

**Current state**

- **Planning is unusually deep for a local tracker:** the workbook port
  supports retirement drawdown, recurring costs, income windows and scenarios.
  This is not a claim of complete parity with the latest freefincal workbook
  or with competitor features that could not be inspected behind login.
- Tracking is shallow:
  - A goal's current corpus is three manual numbers (`current_equity`,
    `current_taxable_fi`, `current_taxfree_fi`). The "prefill from
    portfolio" option copies the *whole* portfolio into one goal. Two goals
    prefilled this way double count.
  - `goal_progress()` uses `recent_monthly_investing()` over all buys across
    all accounts as the "current SIP" for every goal.
  - The goal page's trend chart is the whole-portfolio `ValueSnapshot`
    series, not this goal's corpus.
  - There is no stored expected-path series to compare against actuals, and
    no per-goal asset-allocation drift check against the glide path.

**Fit with local-first**

- **Excellent.** It is all local data and computation, and it is the app's
  core differentiator.

**Recommendation: build**

1. A `GoalAllocation` link from holding (or account) to goal, with a
   percentage. Enforce that each holding's percentages total at most 100%.
   Goal corpus and SIP then come from mapped holdings and their buys.
2. Store the expected corpus path when a plan is saved. Plot actual mapped
   value against it, as the freefincal review tool does.
3. Show current equity/debt split vs the plan's allocation for this year, as
   a rebalancing nudge. Report only; never place trades.

## 6. Family / shared portfolios

**What others do**

- **Kuvera:**
  - Family Account: one login holds individual and joint (anyone-or-survivor)
    accounts for KYC-verified relatives. The 2018 blog says minors and HUF
    were not supported then.
  - Managed Account: someone else's existing Kuvera account can be managed
    from yours; the owner keeps access and approves transactions by OTP.
  - The site still says "Upgrade to Family Account". Blog comments from 2019
    mention it needed Kuvera "coins". Whether it is paid today is unverified.
- **INDmoney:**
  - Invite family members, who approve exactly what they share and can
    revoke it.
  - Combined and per-member net-worth views.
  - Visibility only, with no control over another member's account.
  - Free, per its family MF tracker FAQ.
- **Tickertape:** an App Store release note says "Track your family's
  investments… link multiple accounts & switch between portfolios". I could
  not find a release date or which tier it needs.
- **NaviPlan:** labels per PRAN ("My NPS", "Spouse NPS"), and health-policy
  covered members. That is labelling, not multi-user sharing.

**Current state**

- `Account.owner_label` is a free-text tag set by the one logged-in user. The
  Holdings page can group by "Owner". That is all.
- **This is not shared access.** A spouse cannot log in and see or approve
  anything, and there is no consent or permission model.
- Owner labels do not feed tax maths: FIFO, the ₹1.25L exemption, 80C
  progress and harvesting all pool every owner together. That is wrong when
  labels stand for different PANs.

**Fit with local-first**

- **Household view managed by one person: good.** Its real value is
  per-person tax correctness, which is pure computation.
- **Multi-login sharing with consent and revocation: poor.** It needs
  network exposure, auth hardening and a permission model for a tool meant to
  run on one machine. The current app also keeps some state global (see the
  baseline), so true multi-user use is not safe today.

**Recommendation**

- **Build** an `Owner` (person/PAN) concept that replaces or backs
  `owner_label`. Scope realised and unrealised gains, exemption headroom,
  harvesting and 80C to each owner, and add a per-owner filter on Tax and
  Holdings.
- **Skip** shared logins and invitations. A family that wants two people
  looking at it can share the one local instance.

## 7. Alerts and renewal reminders

**What others do**

- **NaviPlan:** the dashboard shows premiums due within 30 days, with
  Overdue / Due Soon / On Track status, "Mark as Paid" (which rolls the next
  due date forward) and snooze for 1, 7 or 15 days. It runs in the browser,
  so it is in-app only; I found no claim of push or email.
- **INDmoney:** its privacy policy mentions credit-card due-date reminders and
  "alerts/notices". Pending bills, including insurance premiums, show as
  liabilities. Insurance-specific reminders are unverified (§2).
- **Tickertape:** "Basic Alerts" and "Pro Alerts" (price and portfolio). The
  extracted page didn't show exactly what each tier includes.
- **Kuvera:** generic email notifications. I found no renewal-reminder
  feature.

**Current state**

- In-app only, shown when the dashboard is opened:
  - stale or missing prices (`value_gaps`);
  - lots turning long-term within 60 days (`tax_events`);
  - goal status ordering.
- The Protection page shows each policy's next renewal date, flags overdue
  renewals and totals annual premiums. It does **not** send notifications, and nothing else in the
  app does either: no email, push, scheduler or calendar export.
- `update_prices` and snapshots must be run by hand or by cron. The
  Dockerfile and compose file don't schedule them.

**Fit with local-first**

- **In-app lists: excellent.**
- **Push or email:** a background sender needs a running scheduler and SMTP
  credentials. That is acceptable only as opt-in.
- **Calendar export (`.ics`): excellent.** The user's own calendar does the
  reminding. Nothing runs in the background and no credentials are stored.

**Recommendation**

- **Build** one "Upcoming" list on the dashboard covering:
  - policy renewals (from the Protection page) within 30 or 60 days, with
    Mark as paid (rolls to next year) and snooze;
  - FD maturities (if a maturity date field is added);
  - lots turning long-term;
  - FY-end harvesting windows;
  - stale prices.
- **Build** a `.ics` export of renewal and maturity dates.
- **Later:** an optional `manage.py send_reminders` that sends local SMTP
  email from the user's own cron.
- **Skip** price alerts and intraday alerts. They don't suit an
  end-of-day, long-term tracker.

---

## Prioritised backlog

| # | Item | Area | Why first |
|---|---|---|---|
| 1 | Stock/ETF/SGB end-of-day prices from bhavcopy | §3 | Without it, valuations, XIRR, gains and harvesting are wrong for stock holders |
| 2 | Per-buy, per-account FIFO lots, and a "basis unknown" flag for snapshot imports | §4 | Fixes misclassified short/long-term splits and misleading harvest numbers |
| 3 | Owner/PAN scoping for tax and harvesting | §6, §4 | The ₹1.25L exemption and set-off apply per person |
| 4 | Unit-level harvesting plan and CSV export | §4 | Turns aggregate numbers into something the user can act on |
| 5 | Holding-to-goal mapping and expected-vs-actual path | §5 | Removes double counting; matches INDmoney, NaviPlan and the freefincal review tool |
| 6 | Upcoming list and `.ics` export (uses Protection data) | §7 | Reminders without any network service |
| 7 | NPS CRA and EPF passbook parsers | §1 | Removes the main remaining manual balances |
| 8 | Drop-folder CAS import, then optional IMAP | §2 | Cuts down manual uploads without cloud access |

**Explicitly skipped:**

- account aggregator (AA) integration;
- hosted email forwarding and Gmail OAuth via a shared app;
- broker order execution;
- multi-user family sharing;
- stock fundamentals, forecasts and scorecards;
- price and intraday alerts.

## Uncertainty and caveats

- **Paid tiers:**
  - Tickertape: the Pro prices are from its pricing page. Which rows are
    free vs Pro wasn't fully legible in the extracted table.
  - Kuvera Family Account: may or may not need payment or "coins" today.
  - NaviPlan: ₹2,500/year and robo tool ₹5,625 (Sheets edition) as stated on
    freefincal on the check date. Discounts change often.
- **Features that may have changed or that I couldn't confirm:**
  - INDmoney insurance-premium reminders and any harvesting tool.
  - The Tickertape family-portfolio release date and tier.
  - How deep Kuvera's goal tracking goes.
  - Whether Kuvera's `import@kuvera.in` forwarding still works the same way
    (the page is current but undated).
- **Regulatory:** the FIU definition is quoted from the RBI Master Direction
  page; Sahamati's onboarding page says the same. Google's restricted-scope
  exemption details (100-user cap, testing-mode token expiry) are from
  Google's help pages and may change.
- **Code facts** reflect the working tree on 19 October 2026, including the
  Protection page and shared quote links in this change, not a deployed service.

## Sources

All accessed 19 October 2026.

**Kuvera**

- Tax harvesting feature: <https://kuvera.in/feature/tax-harvesting>
- Tax harvesting blog post (July 2026):
  <https://kuvera.in/blog/tax-harvesting-in-mutual-funds-how-it-works-and-why-it-matters/>
- CAS import, family account and managed account overview:
  <https://kuvera.in/why-kuvera/cas-import> (same content at
  <https://kuvera.in/why-kuvera/family-account>)
- Joint and family accounts blog post (2018, with later comments):
  <https://kuvera.in/blog/joint-and-family-accounts-on-kuvera/>

**INDmoney**

- Features overview: <https://www.indmoney.com/features>
- Net worth tracker (depositories and AA, pending bills):
  <https://www.indmoney.com/networth>
- Track external mutual funds (MF Central OTP):
  <https://www.indmoney.com/mutual-funds/track-mutual-funds>
- Family Accounts (consent-based sharing):
  <https://www.indmoney.com/networth/family-accounts>
- Family mutual fund tracker (free):
  <https://www.indmoney.com/features/track-family-mutual-funds>
- Goals tracker: <https://www.indmoney.com/networth/goals-tracker>
- Tax Centre: <https://www.indmoney.com/features/tax-centre>
- Privacy policy (Gmail integration, security assessment):
  <https://www.indmoney.com/privacy-policy>
- User discussion of Gmail access (anecdotal, about March 2026):
  <https://www.reddit.com/r/personalfinanceindia/comments/1s613a5/found_out_indmoney_needs_permanent_gmail_access/>

**Tickertape**

- Pricing and Pro feature table: <https://www.tickertape.in/pricing>
- Pro membership page: <https://www.tickertape.in/membership>
- Analytical tools and data-source disclosure:
  <https://www.tickertape.in/meta/analytical-tools>
- Equity screener: <https://www.tickertape.in/screener/home/equity>
- App Store listing (broker list, family-portfolio release note):
  <https://apps.apple.com/au/app/tickertape-india-us-stocks/id1583214057>

**freefincal / NaviPlan**

- Robo advisory tool: <https://freefincal.com/robo-advisory-software/>
- Robo tool FAQ (pricing, review usage):
  <https://freefincal.com/faq-freefincal-robo-advisor-tool/>
- Equity glide paths update:
  <https://freefincal.com/equity-glide-paths-for-retirement-buckets-freefincal-robo-advisor-update/>
- Goal-based portfolio review tool:
  <https://freefincal.com/a-tool-to-review-your-goal-based-investments/>
- NaviPlan v1.4.0 (insurance, premium reminders, NPS, July 2026):
  <https://freefincal.com/naviplan-v1-4-0-guyot-now-supports-global-assets-nps-pension-funds-and-insurance-polices/>
- NaviPlan v1.6.0 Tax Center (October 2026):
  <https://freefincal.com/naviplan-1-6-0-seamount-a-dedicated-tax-center-and-a-total-net-worth-card/>

**Regulation and platform rules**

- RBI NBFC-Account Aggregator Master Direction (FIU definition):
  <https://www.rbi.org.in/Scripts/BS_ViewMasDirections.aspx?id=10598>
- Sahamati, how to become an FIU:
  <https://sahamati.org.in/financial-information-user-fiu/>
- Google, restricted scope verification and exceptions:
  <https://developers.google.com/identity/protocols/oauth2/production-readiness/restricted-scope-verification>
- Google, when verification is not needed:
  <https://support.google.com/cloud/answer/13464323>
- Gmail API scopes: <https://developers.google.com/gmail/api/auth/scopes>
