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

## 4. What else freefincal looks for, his data sources, and Reddit reception

### Everything he tracks beyond fund metrics

His full toolkit, checked across the site:

- **Goal-based planning first, funds second.** His screener docs say to use it only after: define need and duration, decide asset allocation (robo-advisor tool), decide product category, then screen. Fund picking is step 4 of 4.
- **Portfolio-level review, not fund-level.** Recent article "Stop Comparing Funds; Start Comparing Portfolios" and the portfolio audit tool: backtest the whole portfolio against Nifty 100 or Nifty 500. He repeats "concentrate on the portfolio return, individual funds will have up and down rides."
- **Category-matched benchmarks.** Midcap 150 for midcap funds, Smallcap 250 for smallcap funds, Nifty 50 TRI for largecap. He criticises Morningstar India for using BSE 100 for every fund type.
- **ETF tracking difference**, not just tracking error (a low error can still drift from the benchmark return).
- **Index-category rolling return comparisons** (Nifty 50 vs Next 50 vs Midcap 150 vs Smallcap 250), published monthly.
- **Mandate stability check**: "See if the fund's investment mandate was different in the past. If so, past performance doesn't matter." This is a manual step none of the portals automate.
- **Nifty PE valuation tool** for timing lumpsums, and a moving-average tactical buy/sell tool.
- **His own philosophy**: do not expect returns from SIPs, control the contribution amount instead; annual reviews, ignore daily prices; retirement bucket strategy (low-risk debt buckets).

### Data sources he uses (from his "Data sources for DIY investors" post, May 2024)

- MF NAV history from April 2006: AMFI (via Excel Power Query) or mfapi.in. Same two sources we already use, plus Yahoo Finance for stocks.
- Stocks/ETFs: Moneycontrol historical prices (adjusted for splits/bonus, not dividends) or Yahoo Finance (adjusted for dividends/splits, not bonus). He notes the adjustment gap explicitly.
- NPS NAV: npsnav.in and purifiedbytes.com, both incomplete, "use cautiously".
- Gold prices in INR/USD, petrol/diesel price history, Nifty PE.
- Interactive chart data: scrape the XHR network tab in browser devtools.

So our tigzig Parquet + mfapi tier is already equal or better than his stack. The one thing he has that we do not scrape: fund mandate/change-in-fund-manager history, which he checks manually from AMC fact sheets.

### Reddit reception: is his methodology considered correct?

Reddit blocks full comment-thread scraping, so this is from thread excerpts, AMA posts and search snippets. Directionally consistent across r/IndiaInvestments, r/mutualfunds, r/personalfinanceindia and r/FIRE_Ind.

**Positive**
- r/mutualfunds thread on his lessons: "Freefincal changed the way I look at personal finance in a good way. I learnt about asset allocation, portfolio rebalancing, sequence of returns."
- r/IndianStockMarket: "M. Pattabiraman is my absolutely favourite" (linked from his own profile, so mildly self-selected).
- He has done AMAs on r/IndiaInvestments and r/FIREIndia; r/FIRE_Ind quotes his style as "streamlined investments to goals, saved money like crazy, tempered greed".
- Credibility anchors that even critics accept: nine of his calculators are published on SEBI's investor education portal, and Livemint profiles him twice as a portfolio guru.

**Critical / caveats**
- r/mutualfunds thread "Mr. Pattu is against Small Caps": commenters note he pushes new investors to Nifty 50 index funds and is "against active mutual funds overall". His screener answers this indirectly (it screens active funds fairly), but his advice stance is index-first.
- A professional r/CFP thread (US context, same math) exposed a real technical caveat on capture ratios: with daily return intervals, downside capture is floored at -100 percent so it drifts toward 100 percent regardless of quality, upside capture diverges from 100, and "there is no industry standard method of calculating capture ratios, FactSet uses cumulative returns". Freefincal computes his on monthly CAGRs, which avoids the worst of this interval bias, but it confirms the metric is calculation-sensitive.
- His own posts carry the honest caveats: rolling return consistency "should not be interpreted as a probability of future outperformance", returns vs downside capture show weak correlation with outliers (so he says to read upside and downside separately, not just the ratio), and "good returns imply good downside protection but do not guarantee low stress".

**Net assessment**: his methodology is considered correct and honest by the community. The criticism is about his advice stance (index-first, anti-smallcap for beginners) rather than the math. His own caveats about capture ratios and rolling returns are more conservative than what most portals publish. The one methodological gap found anywhere: capture ratios depend on calculation interval and there is no industry standard, so any capture numbers we compute should state their interval (monthly CAGR, category benchmark) on screen, the way he insists Morningstar's BSE 100 default must be stated.

### What this means for pm-india

Adopt from freefincal, in addition to the metrics already queued (rolling returns, capture ratios, SIP rolling returns, Ulcer Index):

1. Category-matched benchmark mapping for capture ratios, never one index for all funds.
2. Mandate-change warning on the fund analytics page (fund manager change, mandate change, merge history) as a manual-note field, since no API provides it.
3. Portfolio-level rolling return vs NIFTY 500 (we own the benchmark series already), not just per-fund views. This matches his "compare portfolios, not funds" stance and our MPT tools.
4. State the calculation basis on screen wherever capture or consistency scores appear.

## 5. Sources

- freefincal: how upside/downside capture is calculated; how not to use rolling return charts; introduction to capture ratios; equity screener usage guide; SIP rolling returns; data sources for DIY investors (May 2024)
- GitHub repos listed above, plus Yashvi-Vekariya / Mutual-Fund-Predictive-Analytics (surveyed, forecasting on historical NAV, same random-walk limitation)
- mfapi.in docs, AMFI NAV history, jugaad-data docs, fyers API v3 docs, tigzig download endpoint
