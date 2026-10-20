# AGENTS.md — working context for agents and humans

Guidance for any agent working in this repository. Read this before changing
code; it records the project's rules, the reasons behind non-obvious choices,
and the traps that have actually caused bugs.

## What this is

A single-user, self-hosted Django app that tracks Indian investments (mutual
funds, stocks, PPF/EPF, gold, real estate), plans goals with the Freefincal
robo-advisor method, computes tax, and runs a fund consistency screener.
Everything runs on one machine: SQLite (WAL mode), no external services.
The only network calls fetch public price/NAV data and CDN assets.

## Commands

```bash
.venv/bin/python manage.py test core -v 1        # the test suite (~275 tests)
.venv/bin/python manage.py runserver 8300        # dev server
bin/tailwindcss -i static/src/app.css -o static/css/app.css --minify   # after editing static/src/app.css
docker compose build && docker compose up -d     # production-ish run on :8010
```

Deps live in `.venv` (Python 3.12 locally, 3.14 in the container). The Tailwind
build in `bin/tailwindcss` compiles `static/src/app.css` — **edit the source,
never the compiled `static/css/app.css`**.

## Layout

- `core/` — all Django code. Splits roughly into: `portfolio.py` (holdings
  rollup, dashboard PortfolioView), `funds.py` + `fund_analytics.py` (scheme
  research; fund_analytics is a worker-built catalog, do not rebuild lightly),
  `nav_history.py` (DuckDB over `data/amfi_nav_master.parquet`, 37.6M rows —
  refresh weekly, see its docstring), `goals.py` (goal planner), `screener.py`
  (consistency screener engine), `tax/`, `importers/` (CAS PDF, broker and
  bank CSVs), `dashboard_context.py`.
- `templates/` — server-rendered; htmx for search-style partials.
- `docs/` — analysis documents (see below).

## Hard rules and traps

1. **Per-user scoping.** Assets are global (unique on asset_type + identifier);
   ownership is only through `Account.user`. Every money query must filter
   `account__user=…`. `PortfolioView.xirr` once mixed all users' transactions
   and reported -26% on a +20% portfolio — fixed in commit history, regression
   tested in `core/tests_xirr.py`. When you add a rollup, re-check this.
2. **Workbook parity is the spec for goal math.** `core/goals.py` implements
   the Freefincal robo-advisor workbook (July 2025 xlsm). Match its *formula
   logic*, not its stored sample numbers: parameters come from `Assumptions`
   (rates map 1:1 to Settings cells, documented inline). Known workbook bugs
   are NOT copied — implement the intended rule and leave a comment. Full
   evidence: `docs/xlsm-audit-notes.md`, decisions in
   `docs/goal-planner-parity.md`, open gaps in `docs/pending-work.md`.
3. **Scheme-detail cache** (`core/funds.py` `_cached/_store`) holds JSON on
   disk for 12h. If you change the shape of `scheme_detail` output, say so in
   the PR — stale caches keep the old shape until TTL expiry.
4. **Charts.** Chart.js is loaded globally in `base.html`; ECharts 5 is loaded
   per-page where a page needs it (fund_detail, dashboard). Charts read the
   theme once at init — the dark toggle (`base.html`) reloads the page by
   design. Colors follow the semantic palette: emerald positive, red only for
   losses, amber for "your money" overlays. NAV-style single-value series get
   line + gradient area with dataZoom; candlesticks are wrong for NAV (no
   intraday OHLC — see docs/goal-planner-parity.md).
5. **Dark theme** is a palette remap under `html.dark` in
   `static/src/app.css` (Tailwind v4 vars). Add new dark-mode needs there, not
   per-template `dark:` classes. `--color-positive` in dark mode is `#34d399`;
   charts should use the same value.
6. **Transactions drive holdings.** `refresh_holdings()` upserts from
   transactions (average cost). Imports must create Transactions, not Holdings.
7. **Tests**: update existing tests to the corrected spec rather than deleting
   them; regression tests belong through the public interface (views/engines),
   not internals. Sample-case numbers in tests exist to pin formula logic —
   when you change logic deliberately, recompute the expected values and say so.
8. **Deployment**: `docker compose` on :8010, `DEBUG=false`, gunicorn 2
   workers over the volume-mounted `data/` SQLite. Migrations run at container
   start. The dev server on :8300 and the container share the same database
   file — don't run migrations twice concurrently.

## Docs map

- `docs/goal-planner-parity.md` — how the planner matches the Freefincal
  workbook, formula by formula, and what intentionally differs.
- `docs/xlsm-audit-notes.md` — the full extraction: VBA, cell formulas, and
  the Python re-implementation that reproduces the workbook's numbers.
- `docs/pending-work.md` — known gaps and deliberate deviations (kept current).
- `docs/gap-analysis.md` — competitor comparison (Kuvera/INDmoney/Tickertape).
- `docs/mf-forecasting-research.md` — screener workbook deep read.
