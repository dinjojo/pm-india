# PM India: a simple Indian portfolio tracker

A self hosted web app to track your Indian investments. It shows your net worth, helps with tax planning, plans your goals using the Freefincal robo advisor method, and gives you detailed fund analytics. Everything runs on your own machine.

## Features

- **Dashboard**: portfolio value, allocation, diversification, and a trend chart
- **Holdings**: group by asset class, account, owner, or liquidity. Shows key numbers, an allocation bar, and collapsible sections
- **Tax**: realised and unrealised capital gains (FIFO, LTCG/STCG), a tracker for the Rs 1.25L exemption, 80C progress, a real estate sale what if (both tax regimes, Sec 54/54EC), and dividends/IDCW
- **Funds explorer**: browse and filter all 38,000+ AMFI schemes, including matured ones. Live search and fund details
- **Fund analytics**: CAGR, volatility, Sharpe, Sortino, Omega, Calmar, skew, kurtosis, drawdowns, 50/200 DMA buy/sell/hold signals, Monte Carlo VaR and Expected Shortfall, and CAPM alpha/beta against NIFTY 500 TRI
- **Returns calculator**: lumpsum and SIP, shown in nominal and inflation adjusted terms
- **Portfolio tools**: upload a CSV or use your tracked funds to get MPT min variance and max Sharpe optimisation, an efficient frontier, CAPM verdicts, a fund overlap matrix, and a curated basket builder
- **Goals**: a full port of the Freefincal robo advisor workbook. Retirement drawdown with a 4 bucket ladder, income streams with start and stop windows, annuity flooring, scenario analysis, year by year cash flow, and a simple path for people under 26
- **Imports**: CAS statement, photo of any slip using OCR, or manual entry

## Data sources

- **Live NAVs**: mfapi.in (per scheme, cached)
- **Full NAV history**: tigzig AMFI master Parquet (37.6 million rows, all schemes, April 2006 to today), queried directly with DuckDB:

  ```sh
  curl -L -o data/amfi_nav_master.parquet \
       https://api.tigzig.com/mf/v1/download/amfi_nav_master.parquet
  ```

- **Benchmark**: NIFTY 500 TRI via jugaad-data (fetched once, saved to `data/benchmark.csv`)

## Run locally

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
sudo apt install tesseract-ocr          # only needed for the Scan feature
.venv/bin/python manage.py migrate
.venv/bin/python manage.py createsuperuser
.venv/bin/python manage.py runserver
```

## Run in Docker

```sh
docker compose build && docker compose up -d        # runs on :8010
```

## Tests

```sh
.venv/bin/python manage.py test core
```

This app is not SEBI registered and is for informational use only. Your financial data never leaves your machine. The SQLite database and the Parquet file are excluded from git.
