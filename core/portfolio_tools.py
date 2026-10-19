"""Portfolio analysis tools: MPT optimisation, CAPM, return-overlap, curated baskets.

Everything here is pure numpy/pandas on top of the local AMFI NAV Parquet
(core/nav_history.py) — no new dependencies. The NIFTY 500 TRI benchmark comes
from core/fund_analytics.load_benchmark() (cached data/benchmark.csv).

Honest limitation, repeated in the UI: every "expected" return and volatility
is the historical sample mean / covariance over the aligned window. Those are
noisy estimates of the future, and mean-variance optimisers amplify that noise.
"""
from __future__ import annotations

import csv
import io
import os
import re
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd

from core import nav_history

BASE_DIR = os.path.dirname(os.path.dirname(__file__))
BASKET_CACHE_CSV = os.path.join(BASE_DIR, "data", "basket_metrics_cache.csv")

RF = 0.06                 # risk-free rate (annual), ~ Indian T-bill / liquid-fund yield
MIN_HISTORY_YEARS = 1.0   # funds with less NAV history are dropped from the analysis
REDUNDANT_R2 = 0.90       # pairs above this monthly-return R² are flagged redundant
BASKET_CACHE_DAYS = 7
NEAR_CASH_VOL = 0.02     # annual vol below which a fund is treated as cash-like


# ---------------------------------------------------------------------------
# 1. Input: CSV parsing and tracked-holding → AMFI scheme code resolution
# ---------------------------------------------------------------------------

def parse_holdings_csv(text: str) -> tuple[list[tuple[int, float]], list[str]]:
    """Parse "scheme_code,amount" CSV (header row required).

    Columns are located by header name (scheme_code/code/amfi_code, amount/value);
    otherwise column 1 is the code and column 2 the amount. Extra columns are
    ignored. Returns ([(code, amount)], [error strings with line numbers]);
    duplicate codes are summed, order of first appearance kept.
    """
    if not text or not text.strip():
        return [], ["The file is empty — expected a header row then lines like 122639,50000."]
    reader = csv.reader(io.StringIO(text.lstrip("\ufeff")))
    rows = list(reader)
    header = [h.strip().lower() for h in rows[0]]
    code_col = next((i for i, h in enumerate(header) if h in ("scheme_code", "code", "amfi_code", "scheme code")), 0)
    amt_col = next((i for i, h in enumerate(header) if h in ("amount", "value", "current_value", "invested")), 1)
    totals: dict[int, float] = {}
    errors = []
    for lineno, row in enumerate(rows[1:], start=2):
        if not row or not any(c.strip() for c in row):
            continue
        raw_code = row[code_col].strip() if len(row) > code_col else ""
        raw_amt = row[amt_col].strip() if len(row) > amt_col else ""
        if not re.fullmatch(r"\d{3,9}", raw_code):
            errors.append(f"line {lineno}: '{raw_code}' is not an AMFI scheme code (digits only, e.g. 122639)")
            continue
        try:
            amount = float(raw_amt.replace(",", "").replace("₹", ""))
        except ValueError:
            errors.append(f"line {lineno}: amount '{raw_amt}' for scheme {raw_code} is not a number")
            continue
        if amount <= 0:
            errors.append(f"line {lineno}: amount for scheme {raw_code} must be positive")
            continue
        totals[int(raw_code)] = totals.get(int(raw_code), 0.0) + amount
    return list(totals.items()), errors


def scheme_name(code: int) -> str:
    return nav_history.local_scheme_meta(code).get("scheme_name", "") or ""


def resolve_isin(isin: str) -> int | None:
    """ISIN → AMFI scheme code via the Parquet catalog's isin column."""
    if not isin or not nav_history._parquet_exists():
        return None
    rows = nav_history._connect().execute(
        "SELECT scheme_code FROM read_parquet(?) WHERE isin = ? "
        "GROUP BY scheme_code ORDER BY max(date) DESC LIMIT 1",
        [nav_history.NAV_PARQUET, isin.strip().upper()],
    ).fetchall()
    return int(rows[0][0]) if rows else None


_TOKEN_MAP = {"g": "growth", "gr": "growth", "dir": "direct", "pru": "prudential",
              "opp": "opportunities", "opps": "opportunities"}
_STOP = {"fund", "plan", "option", "the", "of", "scheme", "mf", "and", "&"}


def _name_tokens(name: str) -> set[str]:
    s = name.lower()
    s = re.sub(r"(mid|small|large|flexi|multi)cap", r"\1 cap", s)
    toks = re.sub(r"[^a-z0-9]+", " ", s).split()
    return {_TOKEN_MAP.get(t, t) for t in toks} - _STOP


def match_scheme_by_name(name: str, min_score: float = 0.5) -> int | None:
    """Best-effort fuzzy match of a holding name (e.g. CAS "HDFC Mid-Cap
    Opportunities Direct-G") to a currently-reporting AMFI scheme code."""
    if not name or not nav_history._parquet_exists():
        return None
    want = _name_tokens(name)
    if not want:
        return None
    amc = re.sub(r"[^a-z0-9]+", " ", name.lower()).split()[0]
    cutoff = (date.today() - timedelta(days=45)).isoformat()
    rows = nav_history._connect().execute(
        "SELECT scheme_code, any_value(scheme_name), min(date) FROM read_parquet(?) "
        "WHERE lower(scheme_name) LIKE ? GROUP BY scheme_code HAVING max(date) >= ?",
        [nav_history.NAV_PARQUET, f"{amc}%", cutoff],
    ).fetchall()
    best, best_key = None, None
    for code, sname, first in rows:
        low = sname.lower()
        if re.search(r"idcw|dividend|bonus|segregated", low):
            continue
        if ("direct" in want) != ("direct" in low):
            continue
        have = _name_tokens(sname)
        score = len(want & have) / len(want | have)
        key = (score, -int(str(first).replace("-", "")))  # tie → longer history
        if score >= min_score and (best_key is None or key > best_key):
            best, best_key = int(code), key
    return best


def tracked_mf_inputs(user) -> tuple[list[dict], list[str]]:
    """The user's mutual-fund holdings as [{code, amount, name}] (current value),
    summed per scheme across accounts, plus warnings for unresolvable holdings."""
    from core.models import Asset
    from core.portfolio import portfolio_view

    pv = portfolio_view(user)
    out: dict[int, dict] = {}
    warnings = []
    for h in pv.holdings:
        if h.asset_type != Asset.AssetType.MUTUAL_FUND or h.current <= 0:
            continue
        asset = Asset.objects.get(pk=h.asset_pk)
        meta = asset.metadata or {}
        code = None
        for key in ("scheme_code", "amfi_code"):
            if str(meta.get(key, "")).isdigit():
                code = int(meta[key])
        code = code or resolve_isin(asset.isin) or resolve_isin(asset.identifier) \
            or match_scheme_by_name(asset.name)
        if not code:
            warnings.append(f"{asset.name}: could not map to an AMFI scheme code (ISIN "
                            f"'{asset.isin or asset.identifier}') — upload it via CSV instead")
            continue
        row = out.setdefault(code, {"code": code, "amount": 0.0, "name": asset.name})
        row["amount"] += float(h.current)
    return list(out.values()), warnings


# ---------------------------------------------------------------------------
# NAV alignment and per-fund statistics
# ---------------------------------------------------------------------------

def load_price_matrix(codes, history_fn=None, min_years: float = MIN_HISTORY_YEARS):
    """Aligned NAV DataFrame (DatetimeIndex × code) on the intersection of dates.

    Funds with no NAV or less than `min_years` of history are dropped and
    reported as [{code, name, reason}]. If intersecting would shrink the
    common window below `min_years`, the youngest fund is dropped too.
    """
    history_fn = history_fn or nav_history.local_nav_history
    series, dropped = {}, []
    for code in codes:
        rows = history_fn(code)
        if not rows:
            dropped.append({"code": code, "reason": "no NAV history found for this scheme code"})
            continue
        s = pd.Series([n for _, n in rows], index=pd.to_datetime([d for d, _ in rows]), dtype=float)
        s = s[~s.index.duplicated(keep="last")].sort_index()
        s = s[s > 0]
        yrs = (s.index[-1] - s.index[0]).days / 365.25 if len(s) > 1 else 0
        if yrs < min_years:
            dropped.append({"code": code, "reason": f"only {yrs:.1f} years of NAV history "
                                                    f"(need ≥ {min_years:g}y)"})
            continue
        series[code] = s
    while series:
        df = pd.concat(series, axis=1, join="inner").dropna()
        yrs = (df.index[-1] - df.index[0]).days / 365.25 if len(df) > 1 else 0
        if yrs >= min_years or len(series) == 1:
            break
        young = max(series, key=lambda c: series[c].index[0])
        dropped.append({"code": young, "reason": "its history barely overlaps the other funds' "
                                                 "(common window would be under a year)"})
        del series[young]
    else:
        df = pd.DataFrame()
    for d in dropped:
        d.setdefault("name", scheme_name(d["code"]) if history_fn is nav_history.local_nav_history else "")
    return df, dropped


def _years(index) -> float:
    return max((index[-1] - index[0]).days / 365.25, 1e-9)


def fund_metrics(prices: pd.DataFrame) -> dict:
    """{code: {cagr, vol, years}} — CAGR from endpoints, vol from daily returns."""
    yrs = _years(prices.index)
    rets = prices.pct_change().dropna()
    ppy = len(rets) / yrs
    out = {}
    for c in prices.columns:
        cagr = (prices[c].iloc[-1] / prices[c].iloc[0]) ** (1 / yrs) - 1
        out[c] = {"cagr": float(cagr), "vol": float(rets[c].std() * np.sqrt(ppy)), "years": yrs}
    return out


def annualised_moments(returns: pd.DataFrame, periods_per_year: float):
    mu = returns.mean().to_numpy() * periods_per_year
    cov = returns.cov().to_numpy() * periods_per_year
    return mu, cov


def portfolio_stats(w, mu, cov, rf: float = RF) -> dict:
    w = np.asarray(w, dtype=float)
    ret = float(w @ mu)
    vol = float(np.sqrt(max(w @ cov @ w, 0.0)))
    return {"weights": [float(x) for x in w], "ret": ret,
            # geometric (compounded) growth ≈ arithmetic mean − variance/2
            "cagr": ret - vol ** 2 / 2, "vol": vol,
            "sharpe": (ret - rf) / vol if vol > 0 else 0.0}


# ---------------------------------------------------------------------------
# 2. Long-only mean-variance optimisation (numpy only)
# ---------------------------------------------------------------------------

def _project(x: np.ndarray, lower: float = 0.0) -> np.ndarray:
    """Euclidean projection onto {w ≥ lower, Σw = 1} (sort-based simplex projection)."""
    n = len(x)
    radius = 1.0 - n * lower
    v = x - lower
    u = np.sort(v)[::-1]
    css = np.cumsum(u) - radius
    k = np.nonzero(u - css / np.arange(1, n + 1) > 0)[0][-1]
    theta = css[k] / (k + 1)
    return np.maximum(v - theta, 0.0) + lower


def _solve(mu, cov, lam: float, lower: float = 0.0, w0=None, iters: int = 20000, tol: float = 1e-13):
    """min w'Σw − λ·μ'w over {w ≥ lower, Σw = 1} by FISTA (accelerated projected gradient)."""
    n = len(mu)
    step = 1.0 / (2 * max(np.linalg.eigvalsh(cov).max(), 1e-12))
    w = _project(np.full(n, 1.0 / n) if w0 is None else np.asarray(w0, float), lower)
    y, t = w.copy(), 1.0
    for _ in range(iters):
        w_new = _project(y - step * (2 * cov @ y - lam * mu), lower)
        t_new = (1 + np.sqrt(1 + 4 * t * t)) / 2
        y = w_new + (t - 1) / t_new * (w_new - w)
        if np.abs(w_new - w).max() < tol:
            w = w_new
            break
        w, t = w_new, t_new
    return w


def min_variance_weights(cov, lower: float = 0.0) -> np.ndarray:
    cov = np.asarray(cov, float)
    return _solve(np.zeros(len(cov)), cov, 0.0, lower)


def _lambda_sweep(mu, cov, lower: float = 0.0, n: int = 60) -> list[tuple[float, np.ndarray]]:
    """Frontier solutions for λ from 0 (min-var) to huge (max-return), warm-started."""
    scale = float(np.mean(np.diag(cov))) / max(float(np.abs(mu).max()), 1e-9)
    lams = np.r_[0.0, scale * np.logspace(-3, 3.5, n)]
    out, w = [], None
    for lam in lams:
        w = _solve(mu, cov, lam, lower, w0=w, iters=5000, tol=1e-11)
        out.append((float(lam), w))
    # exact max-return corner (long-only: all spare weight on the top-μ asset)
    top = np.full(len(mu), lower)
    top[int(np.argmax(mu))] += 1 - lower * len(mu)
    out.append((float("inf"), top))
    return out


def efficient_frontier(mu, cov, n_points: int = 30, lower: float = 0.0, sweep=None) -> list[dict]:
    """~n_points long-only frontier portfolios evenly spaced in return, from the
    min-variance portfolio to the max-return asset. Weights are linearly
    interpolated between adjacent sweep solutions (the long-only frontier is
    piecewise linear in weights, so this is accurate for a dense sweep)."""
    mu, cov = np.asarray(mu, float), np.asarray(cov, float)
    sweep = sweep or _lambda_sweep(mu, cov, lower)
    pts = [(float(w @ mu), w) for _, w in sweep]
    mono = [pts[0]]
    for r, w in pts[1:]:
        if r > mono[-1][0] + 1e-12:
            mono.append((r, w))
    rets = np.array([r for r, _ in mono])
    if len(mono) == 1:
        targets = [rets[0]]
    else:
        targets = np.linspace(rets[0], rets[-1], n_points)
    out = []
    for t in targets:
        i = int(np.clip(np.searchsorted(rets, t), 1, len(mono) - 1)) if len(mono) > 1 else 0
        if len(mono) > 1:
            (r0, w0), (r1, w1) = mono[i - 1], mono[i]
            a = 0.0 if r1 == r0 else (t - r0) / (r1 - r0)
            w = (1 - a) * w0 + a * w1
        else:
            w = mono[0][1]
        w = w / w.sum()
        out.append({"ret": float(w @ mu), "vol": float(np.sqrt(max(w @ cov @ w, 0))),
                    "weights": [float(x) for x in w]})
    return out


def max_sharpe_weights(mu, cov, rf: float = RF, lower: float = 0.0, sweep=None) -> np.ndarray:
    """Long-only tangency portfolio: best Sharpe on a dense λ sweep, then a
    golden-section refinement on log λ (Sharpe is unimodal along the frontier)."""
    mu, cov = np.asarray(mu, float), np.asarray(cov, float)
    sweep = sweep or _lambda_sweep(mu, cov, lower)

    def sharpe(w):
        v = np.sqrt(max(w @ cov @ w, 1e-18))
        return (w @ mu - rf) / v

    scores = [sharpe(w) for _, w in sweep]
    i = int(np.argmax(scores))
    best_w = sweep[i][1]
    finite = [lam for lam, _ in sweep if np.isfinite(lam) and lam > 0]
    lo_lam = sweep[i - 1][0] if i > 0 else 0.0
    hi_lam = sweep[i + 1][0] if i + 1 < len(sweep) else sweep[i][0]
    if not np.isfinite(hi_lam):
        hi_lam = finite[-1] * 10
    lo, hi = np.log(max(lo_lam, finite[0] / 10)), np.log(max(hi_lam, finite[0]))
    if hi > lo:
        g = (np.sqrt(5) - 1) / 2
        cache = {}

        def f(x):
            if x not in cache:
                cache[x] = _solve(mu, cov, float(np.exp(x)), lower, w0=best_w, iters=20000, tol=1e-13)
            return sharpe(cache[x]), cache[x]

        a, b = lo, hi
        c, d = b - g * (b - a), a + g * (b - a)
        for _ in range(40):
            if f(c)[0] > f(d)[0]:
                b, d = d, c
                c = b - g * (b - a)
            else:
                a, c = c, d
                d = a + g * (b - a)
        cand = f((a + b) / 2)[1]
        if sharpe(cand) > sharpe(best_w):
            best_w = cand
    return best_w / best_w.sum()


# ---------------------------------------------------------------------------
# 3. CAPM
# ---------------------------------------------------------------------------

def estimate_beta(fund_returns, bench_returns) -> float:
    f = np.asarray(fund_returns, float)
    b = np.asarray(bench_returns, float)
    var = np.var(b, ddof=1)
    return float(np.cov(f, b, ddof=1)[0, 1] / var) if var > 0 else float("nan")


def capm_evaluation(fund_cagr: float, beta: float, market_cagr: float, rf: float = RF) -> dict:
    expected = rf + beta * (market_cagr - rf)
    alpha = fund_cagr - expected
    word = "beating" if alpha >= 0 else "behind"
    return {"expected": expected, "alpha": alpha,
            "verdict": f"{word} CAPM by {abs(alpha) * 100:.1f}%/yr"}


def benchmark_name() -> str:
    try:
        from core.fund_analytics import BENCHMARK_NAME
        return BENCHMARK_NAME
    except ImportError:
        return "NIFTY benchmark"


def benchmark_series() -> pd.Series | None:
    """NIFTY 500 TRI (DatetimeIndex → level), reusing core/fund_analytics.py's
    cached loader (data/benchmark.csv, fetched once via
    jugaad_data.nse.index_tri_raw("NIFTY 500", "NIFTY 500", ...) and topped up
    weekly) rather than duplicating the NSE fetch here. None if unavailable."""
    try:
        from core.fund_analytics import load_benchmark
    except ImportError:
        return None
    try:
        return load_benchmark()
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 4. Overlap (returns-based now; holdings-based later)
# ---------------------------------------------------------------------------

def monthly_returns(prices: pd.DataFrame) -> pd.DataFrame:
    return prices.resample("ME").last().pct_change().dropna(how="any")


def return_overlap(prices: pd.DataFrame) -> dict:
    """Pairwise correlation and R² of monthly returns + redundant pairs (R² > 0.9)."""
    m = monthly_returns(prices)
    corr = m.corr().fillna(0.0)
    codes = list(prices.columns)
    r2 = (corr ** 2).to_numpy()
    redundant = []
    for i in range(len(codes)):
        for j in range(i + 1, len(codes)):
            if r2[i, j] > REDUNDANT_R2:
                redundant.append({"a": codes[i], "b": codes[j], "r2": float(r2[i, j])})
    return {"codes": codes, "corr": corr.to_numpy().tolist(), "r2": r2.tolist(),
            "redundant": redundant, "months": len(m)}


def holdings_overlap(code_a: int, code_b: int) -> float:
    """TODO: true stock-level overlap (% of portfolio in common holdings).

    Slot-in point for a holdings feed: MF-Forecast's public RapidAPI
    "indian-mutual-fund-holding" exposes GET /funds/{amfi_code}/holdings where
    amfi_code is the same AMFI scheme code used throughout this module
    (e.g. 122639). Overlap = Σ_stock min(weight_a, weight_b). Needs an API key
    (settings/env) the user doesn't have yet — until then the UI uses
    return_overlap() (return co-movement) instead.
    """
    raise NotImplementedError("holdings feed not configured — see docstring (MF-Forecast API)")


# ---------------------------------------------------------------------------
# Full analysis of one portfolio
# ---------------------------------------------------------------------------

def analyse_portfolio(holdings, rf: float = RF, names: dict | None = None) -> dict:
    """holdings: [(scheme_code, amount)] → everything the portfolio-tools page shows."""
    names = names or {}
    amounts = {}
    for code, amt in holdings:
        amounts[int(code)] = amounts.get(int(code), 0.0) + float(amt)
    prices, dropped = load_price_matrix(list(amounts))
    result = {"dropped": dropped, "funds": [], "error": ""}
    if prices.shape[1] < 2:
        result["error"] = ("Need at least two funds with ≥ 1 year of overlapping NAV history "
                           "to optimise — add more schemes.")
        return result
    codes = list(prices.columns)
    rets = prices.pct_change().dropna()
    yrs = _years(prices.index)
    ppy = len(rets) / yrs
    mu, cov = annualised_moments(rets, ppy)
    metrics = fund_metrics(prices)
    amt = np.array([amounts[c] for c in codes])
    w_cur = amt / amt.sum()
    sweep = _lambda_sweep(mu, cov)
    w_mv = min_variance_weights(cov)
    w_ms = max_sharpe_weights(mu, cov, rf, sweep=sweep)
    portfolios = {"current": portfolio_stats(w_cur, mu, cov, rf),
                  "min_var": portfolio_stats(w_mv, mu, cov, rf),
                  "max_sharpe": portfolio_stats(w_ms, mu, cov, rf)}

    bench = benchmark_series()
    bench_info = None
    capm = {}
    if bench is not None and len(bench):
        joined = prices.join(bench.rename("__bench__"), how="inner").dropna()
        if len(joined) > 200:
            jr = joined.pct_change().dropna()
            jy = _years(joined.index)
            mkt_cagr = (joined["__bench__"].iloc[-1] / joined["__bench__"].iloc[0]) ** (1 / jy) - 1
            bench_info = {"name": benchmark_name(), "cagr": float(mkt_cagr),
                          "start": joined.index[0].date(), "end": joined.index[-1].date(),
                          "data_start": bench.index[0].date(), "data_end": bench.index[-1].date()}
            for c in codes:
                beta = estimate_beta(jr[c], jr["__bench__"])
                fc = (joined[c].iloc[-1] / joined[c].iloc[0]) ** (1 / jy) - 1
                capm[c] = {"beta": beta, **capm_evaluation(fc, beta, mkt_cagr, rf)}

    funds = []
    for i, c in enumerate(codes):
        funds.append({"code": c, "name": scheme_name(c) or names.get(c, str(c)),
                      "amount": amounts[c], **metrics[c], "exp_ret": float(mu[i]),
                      "weight": float(w_cur[i]), "min_var_weight": float(w_mv[i]),
                      "max_sharpe_weight": float(w_ms[i]), "capm": capm.get(c)})
    overlap = return_overlap(prices)
    labels = {c: f["name"] for c, f in zip(codes, funds)}
    overlap["names"] = [labels[c] for c in overlap["codes"]]
    result.update(
        funds=funds, portfolios=portfolios, frontier=efficient_frontier(mu, cov, 30, sweep=sweep),
        window={"start": prices.index[0].date(), "end": prices.index[-1].date(),
                "years": yrs, "obs": len(rets)},
        benchmark=bench_info, overlap=overlap, rf=rf,
        excluded_amount=sum(amounts[d["code"]] for d in dropped if d["code"] in amounts),
    )
    return result


# ---------------------------------------------------------------------------
# 5. Curated basket builder (screening tool)
# ---------------------------------------------------------------------------

_HYBRID = ("hybrid", "balanced", "equity & debt", "equity and debt", "arbitrage", "equity savings",
           "multi asset", "multi-asset", "asset allocation", "dynamic asset", "retirement", "children")
_DEBT = ("liquid", "mmf", "term fund", "government securities", "g-sec", "gsec", "short term", "medium term", "accrual", "cash", "overnight", "money market",
         "gilt", "bond", "debt", "corporate", "banking & psu",
         "banking and psu", "credit risk", "duration", "treasury", "floater", "floating",
         "ultra short", "income", "constant maturity", "savings")
_NOT_POOL = ("gold", "silver", "commodit", "fund of fund", "fof", "overseas", "global", "international",
             "nasdaq", "s&p", "u.s.", "us equity", "world", "emerging", "europe", "japan", "china",
             "idcw", "dividend option", "payout", "bonus", "segregated", "fmp", "fixed maturity",
             "interval", "series", "close ended", "closed ended", "etf", "unclaimed", "target maturity",
             "crisil-ibx", "crisil ibx", "sdl", " plan a", " plan b")


def classify_scheme(name: str) -> str | None:
    """equity | hybrid | debt from scheme-name keywords; None for gold/international/odd."""
    low = name.lower()
    if any(k in low for k in ("gold", "silver", "commodit")):
        return None
    if any(k in low for k in _HYBRID):
        return "hybrid"
    if any(k in low for k in _DEBT):
        return "debt"
    return "equity"


def _catalog_candidates(per_category: int = 50, per_amc: int = 4) -> list[dict]:
    """~150 established direct-growth schemes: ≥5y history, reporting NAVs now,
    no IDCW/FMP/international/oddities; longest track record first, at most
    `per_amc` per AMC per category (AUM isn't in the catalog, so track record +
    AMC spread is the liquidity/robustness proxy)."""
    today = date.today()
    rows = nav_history._connect().execute(
        "SELECT scheme_code, any_value(scheme_name), min(date) FROM read_parquet(?) "
        "WHERE scheme_name ILIKE '%direct%' AND scheme_name ILIKE '%growth%' "
        "GROUP BY scheme_code HAVING max(date) >= ? AND min(date) <= ? ORDER BY min(date), scheme_code",
        [nav_history.NAV_PARQUET, (today - timedelta(days=15)).isoformat(),
         (today - timedelta(days=int(5 * 365.25) + 10)).isoformat()],
    ).fetchall()
    picked, per = [], {}
    for code, name, _first in rows:
        low = name.lower()
        if any(k in low for k in _NOT_POOL):
            continue
        cat = classify_scheme(name)
        if cat is None:
            continue
        amc = low.split()[0]
        if per.get(cat, 0) >= per_category or per.get((cat, amc), 0) >= per_amc:
            continue
        per[cat] = per.get(cat, 0) + 1
        per[(cat, amc)] = per.get((cat, amc), 0) + 1
        picked.append({"code": int(code), "name": name, "category": cat})
    return picked


def _bulk_prices(codes, years: float = 5.0) -> pd.DataFrame:
    since = (date.today() - timedelta(days=int(years * 365.25))).isoformat()
    codes = [int(c) for c in codes]
    if not codes:
        return pd.DataFrame()
    df = nav_history._connect().execute(
        f"SELECT scheme_code, date, CAST(nav AS DOUBLE) AS nav FROM read_parquet(?) "
        f"WHERE scheme_code IN ({','.join('?' * len(codes))}) AND date >= ? AND nav > 0",
        [nav_history.NAV_PARQUET, *codes, since],
    ).df()
    df["date"] = pd.to_datetime(df["date"])
    return df.pivot_table(index="date", columns="scheme_code", values="nav", aggfunc="last").sort_index()


def pool_returns(codes, years: float = 5.0) -> pd.DataFrame:
    """Weekly (Friday) simple returns over the last `years` for the given codes."""
    p = _bulk_prices(codes, years)
    if p.empty:
        return p
    return p.resample("W-FRI").last().ffill().pct_change().iloc[1:]


def basket_candidate_pool(categories) -> list[dict]:
    """Candidate pool with 5y CAGR + vol, cached in data/basket_metrics_cache.csv
    (refreshed when older than 7 days)."""
    cache_ok = os.path.exists(BASKET_CACHE_CSV)
    if cache_ok:
        df = pd.read_csv(BASKET_CACHE_CSV)
        computed = pd.to_datetime(df["computed_at"]).min() if len(df) else None
        cache_ok = computed is not None and datetime.now() - computed < timedelta(days=BASKET_CACHE_DAYS)
    if not cache_ok:
        cands = _catalog_candidates()
        prices = _bulk_prices([c["code"] for c in cands])
        weekly = prices.resample("W-FRI").last().ffill()
        stamp = datetime.now().isoformat(timespec="seconds")
        rows = []
        for c in cands:
            s = prices.get(c["code"])
            if s is None:
                continue
            s = s.dropna()
            if len(s) < 200:
                continue
            yrs = _years(s.index)
            w = weekly[c["code"]].dropna().pct_change().dropna()
            rows.append({**c, "cagr": (s.iloc[-1] / s.iloc[0]) ** (1 / yrs) - 1,
                         "vol": w.std() * np.sqrt(52), "computed_at": stamp})
        df = pd.DataFrame(rows, columns=["code", "name", "category", "cagr", "vol", "computed_at"])
        df.to_csv(BASKET_CACHE_CSV, index=False)
    cats = set(categories or ("equity", "hybrid", "debt"))
    return [r for r in df.to_dict("records") if r["category"] in cats]


def _frontier_pick(mu, cov, target: float, max_vol: float, lower: float):
    """Weights on the long-only frontier: the lowest-vol portfolio reaching
    `target` if that fits under `max_vol`, else the highest-return one at the cap.
    None when even the min-variance portfolio breaks the cap."""
    sweep = _lambda_sweep(mu, cov, lower, n=40)
    pts = [(float(w @ mu), float(np.sqrt(max(w @ cov @ w, 0))), w) for _, w in sweep]
    if pts[0][1] > max_vol + 1e-9:
        return None
    vol_of = lambda w: float(np.sqrt(max(w @ cov @ w, 0)))  # noqa: E731
    prev = pts[0]
    for cur in pts[1:]:
        r, v, w = cur
        if r <= prev[0] + 1e-12:
            continue
        hit_target = r >= target
        hit_cap = v > max_vol
        if hit_target or hit_cap:
            lo, hi = 0.0, 1.0
            if hit_target and (not hit_cap or vol_of(prev[2] + (target - prev[0]) / (r - prev[0]) * (w - prev[2])) <= max_vol):
                a = (target - prev[0]) / (r - prev[0])
                return prev[2] + a * (w - prev[2])
            for _ in range(50):  # bisection along the segment for vol == cap
                mid = (lo + hi) / 2
                if vol_of(prev[2] + mid * (w - prev[2])) <= max_vol:
                    lo = mid
                else:
                    hi = mid
            return prev[2] + lo * (w - prev[2])
        prev = cur
    return prev[2]


def build_basket(pool, returns: pd.DataFrame, periods_per_year: float, target_return: float,
                 max_vol: float, rf: float = RF, max_corr: float = 0.85,
                 min_funds: int = 4, max_funds: int = 6, min_weight: float = 0.05) -> dict:
    """Greedy basket: candidates by Sharpe (desc); add a fund if its correlation
    to every member is < max_corr and the re-optimised basket can stay under
    max_vol; stop at max_funds, or once the target is met with ≥ min_funds.

    Two guards keep near-cash funds (liquid/money-market, whose tiny volatility
    gives them sky-high Sharpe ratios) from filling the basket: at most one fund
    under 2% vol, and while the basket is below target a candidate must lift
    the basket's return. A second pass drops the lift rule only to reach min_funds.
    """
    cands = [p for p in pool if p["code"] in returns.columns and p["vol"] and p["vol"] > 0]
    cands.sort(key=lambda p: (p["cagr"] - rf) / p["vol"], reverse=True)
    basket, weights, cur_ret = [], None, -np.inf
    skipped = {"correlated": 0, "too_volatile": 0, "no_lift": 0}
    tried = set()
    for require_lift in (True, False):
        for p in cands:
            if len(basket) >= max_funds or (len(basket) >= min_funds and (
                    cur_ret >= target_return - 1e-9 or not require_lift)):
                break
            if p["code"] in tried:
                continue
            near_cash = p["vol"] < NEAR_CASH_VOL
            if near_cash and any(b["vol"] < NEAR_CASH_VOL for b in basket):
                continue
            if require_lift and basket and cur_ret < target_return and p["cagr"] <= cur_ret:
                skipped["no_lift"] += 1
                continue
            tried.add(p["code"])
            if basket:
                sub = returns[[b["code"] for b in basket] + [p["code"]]].dropna()
                if len(sub) < 26:
                    continue
                corr = sub.corr().to_numpy()[-1, :-1]
                if np.max(corr) >= max_corr:
                    skipped["correlated"] += 1
                    continue
            trial = basket + [p]
            sub = returns[[b["code"] for b in trial]].dropna()
            mu, cov = annualised_moments(sub, periods_per_year)
            lower = min_weight if len(trial) * min_weight < 1 else 0.0
            w_mv = min_variance_weights(cov, lower)
            if np.sqrt(w_mv @ cov @ w_mv) > max_vol:
                skipped["too_volatile"] += 1
                continue
            w = _frontier_pick(mu, cov, target_return, max_vol, lower)
            if w is None:
                skipped["too_volatile"] += 1
                continue
            basket, weights = trial, w / w.sum()
            cur_ret = float(weights @ mu)
    if not basket:
        return {"funds": [], "ret": 0.0, "vol": 0.0, "cagr": 0.0, "meets_target": False,
                "target": target_return, "max_vol": max_vol, "skipped": skipped,
                "message": "No fund in the pool fits under that volatility cap."}
    sub = returns[[b["code"] for b in basket]].dropna()
    mu, cov = annualised_moments(sub, periods_per_year)
    stats = portfolio_stats(weights, mu, cov, rf)
    funds = [{**b, "weight": float(weights[i]), "exp_ret": float(mu[i]),
              "exp_vol": float(np.sqrt(cov[i, i]))} for i, b in enumerate(basket)]
    funds.sort(key=lambda f: -f["weight"])
    return {"funds": funds, "ret": stats["ret"], "cagr": stats["cagr"], "vol": stats["vol"],
            "sharpe": stats["sharpe"], "meets_target": stats["ret"] >= target_return - 1e-9,
            "target": target_return, "max_vol": max_vol, "skipped": skipped,
            "pool_size": len(cands), "message": ""}


def curated_basket(target_return: float, max_vol: float, categories, rf: float = RF) -> dict:
    pool = basket_candidate_pool(categories)
    rets = pool_returns([p["code"] for p in pool])
    res = build_basket(pool, rets, 52, target_return, max_vol, rf)
    res["categories"] = sorted(set(categories or ()))
    return res
