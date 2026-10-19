"""Scheme analytics (mfforecast / mfanalytics-style) on the local NAV history.

All metrics are computed from the full daily NAV series in the local AMFI
Parquet (core/nav_history.py):

- trailing returns (1M/3M/6M absolute; 1Y+ CAGR) and since-inception CAGR
- daily-return risk: annualised volatility (√252), Sharpe, Sortino, Omega
  (threshold = risk-free), max drawdown with peak/trough/recovery, Calmar,
  skewness, excess kurtosis, best/worst day, positive-day share
- 50/200-DMA crossover signal, rolling 1-year volatility, worst drawdown episodes
- bootstrap Monte Carlo VaR / Expected Shortfall of a one-year lumpsum
- CAPM beta/alpha vs the NIFTY 500 TRI (data/benchmark.csv)

Plus the small future-value maths behind the returns calculator.
Results per scheme are cached for 24h in data/nav_cache (mf_fa_<code>.json).
"""
import csv
import json
import math
import os
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd

from core import funds, nav_history

RF_DEFAULT = 0.06          # annual risk-free rate (≈ 91-day T-bill / repo band)
TRADING_DAYS = 252
MIN_HISTORY_DAYS = 365
CHART_POINTS = 400
ANALYTICS_TTL_HOURS = 24
MC_PATHS = 10_000
CACHE_VERSION = 1

BENCHMARK_CSV = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "benchmark.csv")
# Benchmark: NIFTY 500 Total Return Index from niftyindices.com via
# jugaad_data.nse.index_tri_raw("NIFTY 500", "NIFTY 500", …). TRI (dividends
# reinvested) is the fair comparison for growth-option NAVs. Fetched once
# (2006-04-03 → today, ~5.1k rows) and topped up weekly with the missing tail.
BENCHMARK_NAME = "NIFTY 500 TRI"
BENCHMARK_REFRESH_DAYS = 7
_bench_fetch_failed = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _f(x, nd=None):
    """JSON-safe float (NaN/inf → None), optionally rounded."""
    if x is None:
        return None
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    if math.isnan(x) or math.isinf(x):
        return None
    return round(x, nd) if nd is not None else x


def _iso(ts) -> str | None:
    if ts is None or (isinstance(ts, float) and math.isnan(ts)):
        return None
    return pd.Timestamp(ts).date().isoformat()


def _clean(obj):
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean(v) for v in obj]
    if isinstance(obj, (float, np.floating)):
        return _f(obj)
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, np.bool_):
        return bool(obj)
    return obj


def _decimate_idx(n: int, points: int = CHART_POINTS) -> np.ndarray:
    if n <= points:
        return np.arange(n)
    return np.unique(np.linspace(0, n - 1, points).round().astype(int))


def _cagr(v0: float, v1: float, days: float) -> float | None:
    if v0 <= 0 or v1 <= 0 or days <= 0:
        return None
    return ((v1 / v0) ** (365.25 / days) - 1) * 100


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------
def load_series(code: int):
    """(pd.Series nav indexed by date ascending, meta) or (None, {})."""
    rows = nav_history.local_nav_history(code)
    if not rows:
        return None, {}
    s = pd.Series([n for _, n in rows], index=pd.to_datetime([d for d, _ in rows]), dtype=float)
    s = s[~s.index.duplicated(keep="last")].sort_index()
    s = s[s > 0]
    return s, nav_history.local_scheme_meta(code)


def fetch_benchmark(start: date = date(2006, 4, 1), end: date | None = None) -> list[tuple[str, float]]:
    """NIFTY 500 TRI closes from niftyindices.com via jugaad-data."""
    from jugaad_data.nse import index_tri_raw

    raw = index_tri_raw("NIFTY 500", "NIFTY 500", start, end or date.today())
    out = {}
    for x in raw:
        try:
            out[datetime.strptime(x["Date"], "%d %b %Y").date().isoformat()] = float(x["TotalReturnsIndex"])
        except (KeyError, ValueError, TypeError):
            continue
    return sorted(out.items())


def _write_benchmark(rows):
    tmp = BENCHMARK_CSV + ".tmp"
    with open(tmp, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "value"])
        w.writerows(rows)
    os.replace(tmp, BENCHMARK_CSV)


def load_benchmark() -> pd.Series | None:
    """Benchmark TRI series from data/benchmark.csv; fetches it once if missing
    and tops up the tail when the file is over a week old. None on failure."""
    global _bench_fetch_failed
    existing: list[tuple[str, float]] = []
    if os.path.exists(BENCHMARK_CSV):
        with open(BENCHMARK_CSV, newline="") as f:
            existing = [(r["date"], float(r["value"])) for r in csv.DictReader(f) if r.get("value")]
    if not _bench_fetch_failed:
        try:
            if not existing:
                rows = fetch_benchmark()
                if rows:
                    _write_benchmark(rows)
                    existing = rows
            elif (datetime.now().timestamp() - os.path.getmtime(BENCHMARK_CSV)) > BENCHMARK_REFRESH_DAYS * 86400:
                last = date.fromisoformat(existing[-1][0])
                tail = [r for r in fetch_benchmark(last + timedelta(days=1)) if r[0] > existing[-1][0]]
                existing = existing + tail
                _write_benchmark(existing)  # also bumps mtime so we retry weekly at most
        except Exception:
            _bench_fetch_failed = True
    if not existing:
        return None
    return pd.Series([v for _, v in existing], index=pd.to_datetime([d for d, _ in existing]), dtype=float)


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------
def trailing_returns(s: pd.Series) -> dict:
    last_d, last_v = s.index[-1], s.iloc[-1]
    out = {}
    for label, months in (("1M", 1), ("3M", 3), ("6M", 6), ("1Y", 12), ("3Y", 36), ("5Y", 60)):
        target = last_d - pd.DateOffset(months=months)
        if target < s.index[0]:
            out[label] = None
            continue
        past = s[:target]
        d0, v0 = past.index[-1], past.iloc[-1]
        if months < 12:
            out[label] = _f((last_v / v0 - 1) * 100)
        else:
            out[label] = _f(_cagr(v0, last_v, (last_d - d0).days))
    out["Since inception"] = _f(_cagr(s.iloc[0], last_v, (last_d - s.index[0]).days))
    return out


def drawdown_stats(s: pd.Series) -> tuple[pd.Series, dict, list[dict]]:
    dd = s / s.cummax() - 1
    mdd = float(dd.min())
    info = {"max_drawdown": mdd * 100, "dd_peak": None, "dd_trough": None, "dd_recovery": None,
            "dd_days_to_recover": None}
    if mdd < 0:
        trough = dd.idxmin()
        peak = s[:trough].idxmax()
        after = s[trough:]
        rec = after[after >= s[peak]]
        info.update(dd_peak=_iso(peak), dd_trough=_iso(trough),
                    dd_recovery=_iso(rec.index[0]) if len(rec) else None,
                    dd_days_to_recover=(rec.index[0] - peak).days if len(rec) else None)

    # Underwater episodes: contiguous dd<0 runs, from the prior peak to recovery.
    uw = (dd < 0).values
    episodes = []
    idx = s.index
    i, n = 0, len(s)
    while i < n:
        if not uw[i]:
            i += 1
            continue
        j = i
        while j < n and uw[j]:
            j += 1
        seg = dd.iloc[i:j]
        peak_i = max(i - 1, 0)
        rec_d = idx[j] if j < n else None
        episodes.append({
            "peak": _iso(idx[peak_i]),
            "trough": _iso(seg.idxmin()),
            "recovery": _iso(rec_d) if rec_d is not None else None,
            "depth": float(seg.min()) * 100,
            "length_days": ((rec_d if rec_d is not None else idx[-1]) - idx[peak_i]).days,
            "ongoing": rec_d is None,
        })
        i = j
    episodes.sort(key=lambda e: e["depth"])
    return dd, info, episodes[:5]


def risk_metrics(s: pd.Series, rf: float = RF_DEFAULT) -> dict:
    r = s.pct_change().dropna()
    rf_d = (1 + rf) ** (1 / TRADING_DAYS) - 1
    ex = r - rf_d
    std = r.std()
    vol = std * math.sqrt(TRADING_DAYS)
    downside = math.sqrt(float((np.minimum(ex, 0) ** 2).mean())) * math.sqrt(TRADING_DAYS)
    gains, losses = float(np.maximum(ex, 0).sum()), float(np.maximum(-ex, 0).sum())
    return {
        "volatility": vol * 100,
        "sharpe": (ex.mean() * TRADING_DAYS / vol) if vol > 0 else None,
        "sortino": (ex.mean() * TRADING_DAYS / downside) if downside > 0 else None,
        "omega": (gains / losses) if losses > 0 else None,
        "skewness": float(r.skew()),
        "kurtosis": float(r.kurt()),
        "best_day": float(r.max()) * 100,
        "best_day_date": _iso(r.idxmax()),
        "worst_day": float(r.min()) * 100,
        "worst_day_date": _iso(r.idxmin()),
        "positive_day_pct": float((r > 0).mean()) * 100,
        "days": int(len(r)),
    }


def ma_signal(s: pd.Series) -> tuple[pd.Series, pd.Series, dict]:
    ma50, ma200 = s.rolling(50).mean(), s.rolling(200).mean()
    out = {"signal": None, "ma50": None, "ma200": None, "nav": _f(s.iloc[-1]),
           "last_cross_type": None, "last_cross_date": None}
    if len(s) < 200:
        return ma50, ma200, out
    a, b, nav = float(ma50.iloc[-1]), float(ma200.iloc[-1]), float(s.iloc[-1])
    if a > b and nav > b:
        sig = "BUY"
    elif a < b and nav < b:
        sig = "SELL"
    else:
        sig = "HOLD"
    diff = (ma50 - ma200).dropna()
    sign = np.sign(diff.values)
    nz = np.nonzero(sign)[0]
    cross_type = cross_date = None
    if len(nz) > 1:
        sv = sign[nz]
        changes = np.nonzero(sv[1:] != sv[:-1])[0]
        if len(changes):
            k = nz[changes[-1] + 1]
            cross_type = "golden" if sign[k] > 0 else "death"
            cross_date = _iso(diff.index[k])
    out.update(signal=sig, ma50=a, ma200=b, last_cross_type=cross_type, last_cross_date=cross_date)
    return ma50, ma200, out


def monte_carlo(daily_returns, n_paths: int = MC_PATHS, horizon: int = TRADING_DAYS,
                seed: int = 42, bins: int = 40) -> dict:
    """Bootstrap one-year returns by resampling historical daily returns with
    replacement. Losses are reported as positive percentages."""
    r = np.asarray(daily_returns, dtype=float)
    r = r[np.isfinite(r)]
    rng = np.random.default_rng(seed)
    logr = np.log1p(r)
    total = np.empty(n_paths)
    chunk = 2000  # bound memory: 2000×252 draws at a time
    for start in range(0, n_paths, chunk):
        m = min(chunk, n_paths - start)
        total[start:start + m] = np.expm1(logr[rng.integers(0, len(r), (m, horizon))].sum(axis=1))
    q5, q1 = np.percentile(total, 5), np.percentile(total, 1)
    counts, edges = np.histogram(total * 100, bins=bins)
    centers = (edges[:-1] + edges[1:]) / 2
    return {
        "paths": int(n_paths),
        "horizon_days": int(horizon),
        "var95_pct": float(-q5 * 100),
        "var99_pct": float(-q1 * 100),
        "cvar95_pct": float(-total[total <= q5].mean() * 100),
        "cvar99_pct": float(-total[total <= q1].mean() * 100),
        "median_pct": float(np.median(total) * 100),
        "mean_pct": float(total.mean() * 100),
        "p95_pct": float(np.percentile(total, 95) * 100),
        "prob_loss_pct": float((total < 0).mean() * 100),
        "hist_labels": [round(float(c), 1) for c in centers],
        "hist_counts": [int(c) for c in counts],
    }


def scale_var(mc: dict, amount: float) -> dict:
    """₹ figures for a lumpsum from the percentage VaR/ES."""
    out = {}
    for k in ("var95", "var99", "cvar95", "cvar99"):
        out[f"{k}_inr"] = round(amount * mc[f"{k}_pct"] / 100, 2)
    for k in ("median", "p95"):
        if f"{k}_pct" in mc:
            out[f"{k}_value"] = round(amount * (1 + mc[f"{k}_pct"] / 100), 2)
    return out


def capm(fund: pd.Series, bench: pd.Series | None, rf: float = RF_DEFAULT) -> dict:
    """Beta vs benchmark on overlapping daily returns; CAPM expected return
    rf + β(Rm − rf) with both CAGRs measured over the overlap window."""
    if bench is None or len(bench) == 0:
        return {"ok": False, "reason": f"The {BENCHMARK_NAME} history could not be loaded."}
    df = pd.concat([fund.rename("f"), bench.rename("b")], axis=1, join="inner").dropna()
    if len(df) < 200 or (df.index[-1] - df.index[0]).days < MIN_HISTORY_DAYS:
        return {"ok": False, "reason": f"Less than one year of overlap with the {BENCHMARK_NAME}."}
    rets = df.pct_change().dropna()
    var_b = rets["b"].var()
    if not var_b > 0:
        return {"ok": False, "reason": "Benchmark returns have no variance over the overlap."}
    beta = float(rets["f"].cov(rets["b"]) / var_b)
    corr = float(rets["f"].corr(rets["b"]))
    days = (df.index[-1] - df.index[0]).days
    fund_cagr = _cagr(df["f"].iloc[0], df["f"].iloc[-1], days)
    bench_cagr = _cagr(df["b"].iloc[0], df["b"].iloc[-1], days)
    expected = rf * 100 + beta * (bench_cagr - rf * 100)
    alpha = fund_cagr - expected
    return {
        "ok": True, "benchmark": BENCHMARK_NAME, "beta": beta, "correlation": corr, "r2": corr ** 2,
        "fund_cagr": fund_cagr, "bench_cagr": bench_cagr, "expected": expected, "alpha": alpha,
        "verdict": "beating" if alpha >= 0 else "underperforming",
        "start": _iso(df.index[0]), "end": _iso(df.index[-1]),
    }


def compute_metrics(s: pd.Series, rf: float = RF_DEFAULT, bench: pd.Series | None = None,
                    with_mc: bool = True) -> dict:
    s = s[s > 0].sort_index()
    if len(s) < 2 or (s.index[-1] - s.index[0]).days < MIN_HISTORY_DAYS:
        span = (s.index[-1] - s.index[0]).days if len(s) >= 2 else 0
        return _clean({
            "enough": False,
            "reason": f"Not enough history — only {span} days of NAVs; analytics need at least one year.",
            "start": _iso(s.index[0]) if len(s) else None,
            "end": _iso(s.index[-1]) if len(s) else None,
            "latest_nav": _f(s.iloc[-1]) if len(s) else None,
        })
    rets = trailing_returns(s)
    dd, dd_info, episodes = drawdown_stats(s)
    risk = risk_metrics(s, rf)
    risk.update(dd_info)
    cagr = rets["Since inception"]
    risk["calmar"] = (cagr / abs(risk["max_drawdown"])) if risk["max_drawdown"] < 0 and cagr is not None else None
    ma50, ma200, ma = ma_signal(s)

    ix = _decimate_idx(len(s))
    chart = {
        "dates": [_iso(d) for d in s.index[ix]],
        "nav": [_f(v, 4) for v in s.values[ix]],
        "ma50": [_f(v, 4) for v in ma50.values[ix]],
        "ma200": [_f(v, 4) for v in ma200.values[ix]],
        "drawdown": [_f(min(v * 100, 0.0), 2) for v in dd.values[ix]],
    }
    daily = s.pct_change().dropna()
    rv = (daily.rolling(TRADING_DAYS).std() * math.sqrt(TRADING_DAYS) * 100).dropna()
    rix = _decimate_idx(len(rv))
    rolling_vol = {"dates": [_iso(d) for d in rv.index[rix]], "vol": [_f(v, 2) for v in rv.values[rix]]}

    out = {
        "enough": True,
        "start": _iso(s.index[0]), "end": _iso(s.index[-1]), "latest_nav": _f(s.iloc[-1]),
        "observations": int(len(s)), "years": round((s.index[-1] - s.index[0]).days / 365.25, 1),
        "rf_pct": rf * 100,
        "returns": rets, "risk": risk, "ma": ma, "drawdowns": episodes,
        "chart": chart, "rolling_vol": rolling_vol,
        "capm": capm(s, bench, rf),
    }
    if with_mc:
        out["mc"] = monte_carlo(daily.values)
    return _clean(out)


# ---------------------------------------------------------------------------
# Cached per-scheme entry point
# ---------------------------------------------------------------------------
def _cache_path(code: int) -> str:
    return os.path.join(funds.CACHE_DIR, f"mf_fa_{code}.json")


def scheme_analytics(code: int, rf: float = RF_DEFAULT) -> dict | None:
    """Full analytics for one scheme (24h disk cache). None if unknown."""
    path = _cache_path(code)
    if os.path.exists(path) and (datetime.now().timestamp() - os.path.getmtime(path)) < ANALYTICS_TTL_HOURS * 3600:
        try:
            with open(path) as f:
                cached = json.load(f)
            if cached.get("_v") == CACHE_VERSION and cached.get("rf_pct") in (None, rf * 100):
                return cached
        except (OSError, ValueError):
            pass
    s, meta = load_series(code)
    if s is None or len(s) == 0:
        return None
    try:
        bench = load_benchmark()
    except Exception:
        bench = None
    out = compute_metrics(s, rf=rf, bench=bench)
    name = (meta or {}).get("scheme_name") or f"Scheme {code}"
    out.update({"_v": CACHE_VERSION, "code": code, "name": name, "isin": (meta or {}).get("isin_growth", ""),
                **funds.parse_scheme_name(name)})
    out.setdefault("rf_pct", rf * 100)
    funds._store(f"fa_{code}", out)
    return out


# ---------------------------------------------------------------------------
# Returns calculator maths
# ---------------------------------------------------------------------------
def lumpsum_fv(amount: float, rate_pct: float, years: int, inflation: float = 6.0) -> dict:
    r, inf = rate_pct / 100, inflation / 100
    series = [{"year": y, "invested": amount, "nominal": amount * (1 + r) ** y,
               "real": amount * (1 + r) ** y / (1 + inf) ** y} for y in range(years + 1)]
    end = series[-1]
    return {"invested": amount, "nominal": end["nominal"], "real": end["real"],
            "gain": end["nominal"] - amount, "series": series}


def sip_fv(monthly: float, rate_pct: float, years: int, inflation: float = 6.0) -> dict:
    """Monthly SIP at the start of each month, compounding monthly at rate/12
    (the convention used by AMC/Groww SIP calculators)."""
    i, inf = rate_pct / 100 / 12, inflation / 100

    def fv(months):
        if months == 0:
            return 0.0
        return monthly * months if i == 0 else monthly * ((1 + i) ** months - 1) / i * (1 + i)

    series = [{"year": y, "invested": monthly * 12 * y, "nominal": fv(12 * y),
               "real": fv(12 * y) / (1 + inf) ** y} for y in range(years + 1)]
    end = series[-1]
    return {"invested": end["invested"], "nominal": end["nominal"], "real": end["real"],
            "gain": end["nominal"] - end["invested"], "series": series}
