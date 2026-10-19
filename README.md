# PM India — a simple Indian portfolio tracker

Self-hosted web app to track mutual funds, stocks and other Indian investments: net-worth dashboard, tax planning, goal planning (Freefincal robo-advisor methodology) and quantitative fund analytics — entirely on your own machine.

## Features

- **Dashboard** — portfolio value, allocation, diversification, Tickertape-style trend chart
- **Holdings** — grouped by asset class / account / owner / liquidity, KPIs, allocation bar, collapsible sections
- **Tax** — realised & unrealised capital gains (FIFO, LTCG/STCG), ₹1.25L headroom tracker, 80C progress, real-estate sale what-if (dual regime, Sec 54/54EC), dividends/IDCW
- **Funds explorer** — browse & filter all 38,000+ AMFI schemes (incl. matured), live search, fund detail
- **Fund analytics** — CAGR, volatility, Sharpe / Sortino / Omega / Calmar, skew & kurtosis, drawdowns, 50/200-DMA buy/sell/hold signals, Monte Carlo VaR & Expected Shortfall, CAPM alpha/beta vs NIFTY 500 TRI
- **Returns calculator** — lumpsum & SIP, nominal vs inflation-adjusted
- **Portfolio tools** — CSV upload or tracked funds → MPT min-variance / max-Sharpe optimisation, efficient frontier, CAPM verdicts, fund-overlap matrix, curated basket builder
- **Goals** — full port of the Freefincal robo-advisor workbook: retirement drawdown with a 4-bucket ladder, income streams with start/stop windows, annuity flooring, scenario analysis, year-by-year cash flow, under-26 path
- **Imports** — CAS statement, photo/OCR of any slip, manual entry

## Data

- **Live NAVs**: mfapi.in (per-scheme, cached)
- **Full NAV history**: tigzig AMFI master Parquet (37.6M rows, all schemes, Apr 2006 → today), queried in place with DuckDB:

  ```sh
  curl -L -o data/amfi_nav_master.parquet \
       https://api.tigzig.com/mf/v1/download/amfi_nav_master.parquet
  ```

- **Benchmark**: NIFTY 500 TRI via jugaad-data (fetched once, cached to `data/benchmark.csv`)

## Run locally

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
sudo apt install tesseract-ocr          # Scan feature only
.venv/bin/python manage.py migrate
.venv/bin/python manage.py createsuperuser
.venv/bin/python manage.py runserver
```

## Run in Docker

```sh
docker compose build && docker compose up -d        # :8010
```

## Tests

```sh
.venv/bin/python manage.py test core
```

Not SEBI-registered; informational use only. Your financial data never leaves the machine — the SQLite DB and Parquet are gitignored.
