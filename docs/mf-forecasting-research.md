# MF Forecasting and Historical Data Research

This document records all the research done in this project on mutual fund historical data, prediction models, and performance analytics. It is the reference for why the app is built the way it is, and what to add next.

## 1. Historical NAV data: final decision

We compared four sources.

| Source | What it gives | Verdict |
|---|---|---|
| tigzig `amfi_nav_master.parquet` | All 38,289 schemes, 37.6M rows, Apr 2006 to today, including matured/closed funds. Weekly refresh download. | Primary. Local DuckDB queries over it (`core/nav_history.py`). |
| mfapi.in | Live per-scheme NAV history via `/mf/<scheme_code>` | Secondary. Used for fresh data and the fund search flow. |
| mfnav | NAV history API | Dropped. No advantage over tigzig + mfapi. |
| jugaad-data | Stock and index data, NIFTY 500 TRI | Only used for the benchmark series (`data/benchmark.csv`). |

Conclusion: two-tier design. Tier A is the local Parquet file for everything historical. Tier B is mfapi.in for live per-scheme updates. AMFI's own NAV history page and the freefincal AMFI downloader were checked but both are worse than the Parquet bundle for bulk work.

Dividend payout history for MFs has no free source. mfapi is NAV-only, Value Research blocks scraping. Dividend/IDCW payout data comes from CAS imports, with a link-out to Value Research.

## 2. Prediction research: what we studied

### The core finding

NAV levels are close to a random walk. Across every repo and paper we reviewed, LSTM and deep models did not honestly beat a naive baseline (tomorrow's NAV equals today's NAV) once you evaluate walk-forward instead of on the training set. Small reported MAPE wins came from predicting a value very close to yesterday's price.

### GitHub repos reviewed in depth

1. **srinivasRM / Mutual-funds-Analysis-and-prediction** (15 stars)
   Random Forest on fund attributes (category, AUM, expense ratio, past returns) to predict cross-sectional 1/3/5 year returns. Cross-sectional ranking is more defensible than time series prediction, but sample is small and there is no walk-forward validation.

2. **MohitNaveen / Investment-Analytics-Dashboard-with-ML-Forecasting**
   The most rigorous ML methodology of the four: 30 lag features, recursive multi-step forecasting, 95 percent confidence bands, holdout MAPE. Still predicts NAV levels, so most of its accuracy is the naive baseline in disguise.

3. **Abhishek-Arora / Mutual-Fund-Performance-Prediction**
   AMFI plus MoneyControl data, category-relative features. Honest about class imbalance when classifying funds that beat their category. Good feature engineering ideas, no live walk-forward scoring.

4. **vijaybkhot / material-forecasting-engine**
   Best architecture, not MF specific: offline training pipeline, S3 artifact store, cache layer, FastAPI serving, per-series model selection, MODEL_CARD.md per model. This is the shape we would copy if we ever serve models, not the math inside it.

### mfforecast.com

The only genuinely predictive product we found. It rebuilds a fund's intraday value from its holdings and live stock prices, and reaches about 0.96 correlation with the official NAV before it is published. This is nowcasting, not forecasting. It needs a holdings API (MF-Forecast RapidAPI, free tier). The app already has a seam for it: the `holdings_overlap` stub in `core/portfolio_tools.py`.

### freefincal (Dr M. Pattabiraman, IIT Madras)

His method is not prediction at all. It is consistency measurement from history:

- **Rolling returns**: compute CAGR over every possible 3Y/5Y/7Y window since Apr 2006, shifting the start date each day. Consistency score = fraction of windows where the fund beat its benchmark (for example 675/876 = 77 percent). He explicitly warns not to read this as a probability of future outperformance.
- **Upside/downside capture ratios** (his favourite): annualised fund return in index-up months divided by index return in those months, same for down months. Upside above 100 and downside below 100 is ideal. He moved away from standard deviation, alpha and beta as risk measures because of this.
- **Ulcer Index** as his alternative risk measure.
- **SIP rolling returns**: roll an entire SIP across start dates and XIRR each one. Computationally heavy, which is why portals do not show it.
- His monthly screener datasheet publishes: downside capture, upside capture, capture ratio, SIP returns 1 to 9 years, lumpsum returns 1 to 9 years, for 480+ funds.
- His data sources are the same as ours: AMFI NAV history (from April 2006), mfapi.in, Yahoo Finance.

## 3. Verdict and recommendation

**Do not build an LSTM/NAV forecaster.** It cannot honestly beat the naive baseline and would mislead users.

What is worth building, in order:

1. **Forecast card (approved direction)**: exponential smoothing plus SMA ensemble, 1 to 15 day horizon, drawn as a fan chart of percentile bands, never a single line. It carries a permanent skill-vs-naive scorecard and hides itself whenever skill is zero or negative. Users see uncertainty, not a fake prediction.
2. **Holdings-based nowcast card**: fund value today from holdings and live prices, once we get an MF-Forecast RapidAPI key. Free tier.
3. **Freefincal-style analytics on the fund analytics page** (we already have CAGR, vol, Sharpe/Sortino/Omega/Calmar, skew/kurtosis, max drawdown, 50/200 DMA signals, Monte Carlo VaR/ES, CAPM vs NIFTY 500 TRI):
   - Rolling return chart with consistency score vs benchmark.
   - Rolling upside/downside capture chart.
   - SIP rolling returns (XIRR across rolled SIPs), which no Indian portal shows.
   - Ulcer Index.
4. **TruthLens rule (permanent)**: any model or signal we show must beat the naive baseline on walk-forward data or it does not ship.

## 4. Sources

- freefincal: how upside/downside capture is calculated; how not to use rolling return charts; introduction to capture ratios; equity screener usage guide; SIP rolling returns; data sources for DIY investors (May 2024)
- GitHub repos listed above, plus Yashvi-Vekariya / Mutual-Fund-Predictive-Analytics (surveyed, forecasting on historical NAV, same random-walk limitation)
- mfapi.in docs, AMFI NAV history, jugaad-data docs, fyers API v3 docs, tigzig download endpoint
