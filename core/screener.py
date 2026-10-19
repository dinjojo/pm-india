"""Freefincal-style equity screener, rebuilt for the web.

Design consensus lives in docs/mf-forecasting-research.md section 5. The core
idea, taken from AUG-2021-freefincal-Equity-mutual-fund-outperformance-macro-
screener.xlsm, is: precompute every metric offline, then filtering is a plain
query (his Excel macros only applied AutoFilter over precomputed columns).

Metrics per scheme x benchmark x window (1Y/2Y/3Y/4Y/5Y), over MONTHLY
month-end NAVs rolled one month at a time since April 2006:

- ro  rolling-return outperformance consistency = share of rolling windows
      where the fund CAGR beat the benchmark CAGR
- up  upside performance consistency = same share restricted to windows where
      the benchmark CAGR was positive
- dc  downside protection consistency = same share restricted to windows where
      the benchmark CAGR was negative
- tr/tb  trailing fund/benchmark CAGR over the window
- ex  excess return = tr - tb
- sd/sdb/exr  monthly-return stdev of fund, of benchmark, and their difference

The denominator is always the fund's own aligned window count (the workbook
had 8 rows where index and fund counts diverged by a few months; aligning the
two series first removes that artifact).

Benchmark policy: every category gets two benchmark variants, a market index
and a blended hybrid index, mirroring the workbook's Nifty 100 TRI + Crisil
65:35 pairing. We only hold the NIFTY 500 TRI series locally (data/
benchmark.csv), so additional NSE TRIs are fetched once via jugaad-data and
cached as data/benchmark_<key>.csv; if a fetch fails the category falls back
to NIFTY 500 TRI. The Crisil 65:35 hybrid proxy is built locally as
0.65 x equity index + 0.35 x a liquid-fund NAV proxy from the Parquet,
rebalanced monthly on returns (crisp 65% equity / 35% bonds hybrid index is
not freely available).
"""
import os
from datetime import date

import numpy as np
import pandas as pd

from core import nav_history

BASE_DIR = os.path.dirname(os.path.dirname(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
BENCHMARK_CSV = os.path.join(DATA_DIR, "benchmark.csv")
DEBT_PROXY_CSV = os.path.join(DATA_DIR, "debt_proxy.csv")
SCREENER_PARQUET = os.path.join(DATA_DIR, "screener_metrics.parquet")

WINDOWS = {12: "1Y", 24: "2Y", 36: "3Y", 48: "4Y", 60: "5Y"}
START = date(2006, 4, 1)
MIN_MONTHS = 70          # a scheme needs at least ~6 years to be screenable
MIN_DAILY_POINTS = 1000
STALE_DAYS = 30          # last NAV within this many days of the newest = active

# --------------------------------------------------------------------------
# Category mapping: keyword match on the lowercase scheme name, most specific
# first. Categories match the freefincal workbook; anything unmatched, and
# everything that is clearly debt / index / ETF / arbitrage / FoF, is skipped.
# Each category carries (default benchmark key, blended benchmark key or None).
# --------------------------------------------------------------------------
SKIP_KEYWORDS = [
    "index fund", " index", "etf", "arbitrage", "fund of fund", "fof",
    "gold", "silver", "debt", "gilt", "income fund", "liquid", "bond",
    "money maker", "treasury", "savings fund", "conservative hybrid",
    "equity savings", "dynamic asset allocation", "balanced advantage",
]

CATEGORY_MAP = [
    ("Aggressive Hybrid Fund",
     ["aggressive hybrid", "equity hybrid", "equity & bond", "equity and bond",
      "balanced fund", "hybrid equity"]),
    ("Multi Cap Fund", ["multi cap", "multicap"]),
    ("Large & Mid Cap Fund", ["large and mid", "large & mid", "largemidcap",
                              "core equity", "emerging leaders"]),
    ("Large Cap Fund", ["large cap", "largecap", "bluechip", "top 100",
                        "nifty 100", "large50", "equity large"]),
    ("Mid Cap Fund", ["mid cap", "midcap", "midcap 150"]),
    ("Small Cap Fund", ["small cap", "smallcap", "smallcap 250"]),
    ("Dividend Yield Fund", ["dividend yield"]),
    ("ELSS", ["elss", "tax saver", "taxsaver", "tax relief", "taxshield"]),
    ("Focussed Fund", ["focused", "focussed"]),
    ("Value Fund", ["value fund", "value discovery", "value & contrast"]),
    ("Contra Fund", ["contra"]),
    ("Flexi Cap Fund", ["flexi cap", "flexicap", "flexible", "equity fund",
                        "opportunities fund", "emerging equity", "equity opportunities",
                        "leadership equity", "growth fund"]),
    ("Sectoral/Thematic", ["sector", "thematic", "pharma", "health", "banking",
                           "financial", "technology", "tech fund", "digital",
                           "infrastructure", "consumption", "psu", "manufacturing",
                           "transport", "real estate", "energy", "auto", "metal",
                           "fmcg", "innovation", "special opportunit", "business cycle",
                           "quant", "esg", "housing", "rural", "exports"]),
]

# category -> (default benchmark, blended alternative benchmark)
BENCHMARK_MAP = {
    "Large Cap Fund": ("nifty100_tri", None),
    "Large & Mid Cap Fund": ("largemid250_tri", None),
    "Mid Cap Fund": ("midcap150_tri", None),
    "Small Cap Fund": ("smallcap250_tri", None),
    "Flexi Cap Fund": ("nifty500_tri", None),
    "Multi Cap Fund": ("nifty500_tri", None),
    "ELSS": ("nifty500_tri", None),
    "Focussed Fund": ("nifty500_tri", None),
    "Value Fund": ("nifty500_tri", None),
    "Contra Fund": ("nifty500_tri", None),
    "Dividend Yield Fund": ("nifty500_tri", None),
    "Sectoral/Thematic": ("nifty500_tri", None),
    "Aggressive Hybrid Fund": ("nifty100_tri", "crisil6535"),
}

# key -> jugaad-data index_tri_raw(name, tri_name) parameters
JUGAAD_INDICES = {
    "nifty500_tri": ("NIFTY 500", "NIFTY 500"),
    "nifty100_tri": ("NIFTY 100", "NIFTY 100"),
    "largemid250_tri": ("NIFTY LARGEMIDCAP 250", "NIFTY LARGEMIDCAP 250"),
    "midcap150_tri": ("NIFTY MIDCAP 150", "NIFTY MIDCAP 150"),
    "smallcap250_tri": ("NIFTY SMALLCAP 250", "NIFTY SMALLCAP 250"),
}
BENCHMARK_LABELS = {
    "nifty500_tri": "NIFTY 500 TRI",
    "nifty100_tri": "NIFTY 100 TRI",
    "largemid250_tri": "NIFTY LARGEMIDCAP 250 TRI",
    "midcap150_tri": "NIFTY MIDCAP 150 TRI",
    "smallcap250_tri": "NIFTY SMALLCAP 250 TRI",
    "crisil6535": "Hybrid 65:35 (NIFTY 100 TRI + liquid debt proxy)",
}

_blend_debt = None


# --------------------------------------------------------------------------
# Benchmark series
# --------------------------------------------------------------------------
def _month_end(df: pd.DataFrame, col: str = "value") -> pd.Series:
    """Month-end series from a DataFrame with a DatetimeIndex."""
    s = df[col].copy()
    s.index = pd.to_datetime(s.index)
    return s.sort_index().resample("ME").last().dropna()


def load_nifty500_tri() -> pd.Series:
    df = pd.read_csv(BENCHMARK_CSV, parse_dates=["date"]).set_index("date")
    return df["value"].astype(float)


def _debt_proxy() -> pd.Series:
    """Monthly-return series of a liquid-fund NAV from the Parquet (35% leg)."""
    global _blend_debt
    if _blend_debt is not None:
        return _blend_debt
    if os.path.exists(DEBT_PROXY_CSV):
        df = pd.read_csv(DEBT_PROXY_CSV, parse_dates=["date"]).set_index("date")
        _blend_debt = df["value"].astype(float)
        return _blend_debt
    con = nav_history._connect()
    rows = con.execute(
        "SELECT scheme_code, max(scheme_name) AS scheme_name "
        "FROM read_parquet(?) "
        "WHERE (lower(scheme_name) LIKE '%liquid fund%' OR lower(scheme_name) LIKE '%liquid fund -%') "
        "  AND lower(scheme_name) LIKE '%growth%' "
        "GROUP BY scheme_code ORDER BY count(*) DESC LIMIT 20",
        [nav_history.NAV_PARQUET],
    ).fetchall()
    if not rows:
        raise RuntimeError("no liquid-fund scheme found for the debt proxy")
    # A liquid fund's NAV should never move more than a percent or two in a
    # month; AMFI does have resets/mergers that look like a 90% crash. Skip
    # any candidate with such a discontinuity instead of blending it in.
    for code, _name in rows:
        hist = con.execute(
            "SELECT date, nav FROM read_parquet(?) WHERE scheme_code = ? AND nav IS NOT NULL ORDER BY date",
            [nav_history.NAV_PARQUET, code],
        ).fetchall()
        df = pd.DataFrame(hist, columns=["date", "nav"])
        df["date"] = pd.to_datetime(df["date"])
        nav = df.set_index("date")["nav"].astype(float)
        monthly = _month_end(nav.to_frame("value"))
        if len(monthly) < MIN_MONTHS:
            continue
        rets = monthly.pct_change().dropna()
        if (rets.abs() > 0.10).any():
            continue
        monthly.to_frame("value").rename_axis("date").to_csv(DEBT_PROXY_CSV)
        _blend_debt = monthly
        return _blend_debt
    raise RuntimeError("no clean liquid-fund series found for the debt proxy")


def _fetch_jugaad_index(key: str) -> pd.Series | None:
    try:
        from jugaad_data.nse import index_tri_raw
        name, tri = JUGAAD_INDICES[key]
        # jugaad returns a list of dicts and both dates must be date objects
        # (a string to_date makes it crash deep inside ut.break_dates).
        rows = index_tri_raw(name, tri, from_date=date(2006, 3, 1),
                             to_date=date.today())
        if not rows or len(rows) < 500:
            return None
        df = pd.DataFrame(rows)
        dates = pd.to_datetime(df["Date"], format="%d %b %Y", errors="coerce")
        vals = pd.to_numeric(df["TotalReturnsIndex"], errors="coerce")
        s = pd.Series(vals.to_numpy(), index=dates.to_numpy()).dropna()
        if len(s) < 500:
            return None
        return s.sort_index()
    except Exception:
        return None


def load_benchmark(key: str) -> tuple[pd.Series | None, str]:
    """Month-end daily series for a benchmark key, plus the label actually used.

    Returns (None, fallback_label) when the series cannot be provided."""
    label = BENCHMARK_LABELS.get(key, key)
    if key == "nifty500_tri":
        return load_nifty500_tri(), label
    if key == "crisil6535":
        return None, label          # built by build_crisil_proxy, needs equity series
    path = os.path.join(DATA_DIR, f"benchmark_{key}.csv")
    if os.path.exists(path):
        df = pd.read_csv(path, parse_dates=["date"]).set_index("date")
        return df["value"].astype(float), label
    s = _fetch_jugaad_index(key)
    if s is None:
        return None, BENCHMARK_LABELS["nifty500_tri"]
    s.to_frame("value").rename_axis("date").to_csv(path)
    return s, label


def build_crisil_proxy(equity: pd.Series) -> pd.Series | None:
    """65/35 hybrid proxy: monthly returns blended, i.e. rebalanced monthly."""
    try:
        debt = _debt_proxy()
    except Exception:
        return None
    eq_m = _month_end(equity.to_frame("value")) if equity.index.inferred_freq != "ME" \
        else equity
    er = eq_m.pct_change().dropna()
    dr = debt.pct_change().dropna()
    joined = pd.concat([er, dr], axis=1, join="inner").dropna()
    if len(joined) < MIN_MONTHS:
        return None
    blend = 0.65 * joined.iloc[:, 0] + 0.35 * joined.iloc[:, 1]
    return 100 * (1 + blend).cumprod()


# --------------------------------------------------------------------------
# Universe selection
# --------------------------------------------------------------------------
def categorize(name: str) -> str | None:
    n = name.lower()
    if any(k in n for k in SKIP_KEYWORDS):
        return None
    for cat, kws in CATEGORY_MAP:
        if any(k in n for k in kws):
            return cat
    return None


def universe() -> pd.DataFrame:
    """Active equity/hybrid schemes worth screening, with their category."""
    con = nav_history._connect()
    df = con.execute(
        """
        SELECT scheme_code, max(scheme_name) AS scheme_name, min(date) AS first_date,
               max(date) AS last_date, count(*) AS n
        FROM read_parquet(?)
        GROUP BY scheme_code
        HAVING count(*) >= ? AND min(date) <= ?
        """,
        [nav_history.NAV_PARQUET, MIN_DAILY_POINTS, str(date(2019, 1, 1))],
    ).fetchdf()
    latest = pd.to_datetime(df["last_date"]).max()
    df = df[pd.to_datetime(df["last_date"]) >= latest - pd.Timedelta(days=STALE_DAYS)]
    df["category"] = df["scheme_name"].map(categorize)
    df = df[df["category"].notna()].copy()
    df["scheme_code"] = df["scheme_code"].astype(int)
    return df[["scheme_code", "scheme_name", "category", "n"]].reset_index(drop=True)


# --------------------------------------------------------------------------
# Metric computation
# --------------------------------------------------------------------------
def rolling_cagrs(monthly_nav: pd.Series, w: int) -> np.ndarray:
    """CAGR of every w-month window, rolled one month at a time."""
    v = monthly_nav.values.astype(float)
    if len(v) <= w:
        return np.array([])
    if (v <= 0).any():
        # zero/blank NAVs are bad AMFI data; drop them so log() stays finite
        v = v[v > 0]
        if len(v) <= w:
            return np.array([])
    logs = np.log(v)
    win = np.lib.stride_tricks.sliding_window_view(logs, w + 1)
    total = win[:, -1] - win[:, 0]
    return np.expm1(total / (w / 12.0))


def scheme_metrics(code: int, benchmarks: dict[str, pd.Series]) -> list[dict]:
    """Rows for one scheme: one per benchmark series it can be aligned with."""
    hist = nav_history.local_nav_history(code)
    if len(hist) < MIN_DAILY_POINTS:
        return []
    daily = pd.DataFrame(hist, columns=["date", "nav"])
    daily["date"] = pd.to_datetime(daily["date"])
    daily = daily.set_index("date")["nav"].astype(float)
    daily = daily[daily > 0]        # AMFI occasionally reports 0; log breaks on it
    monthly = _month_end(daily.to_frame("value"))
    monthly = monthly[monthly.index >= pd.Timestamp(START)]

    out = []
    for key, bench_daily in benchmarks.items():
        bench_monthly = _month_end(bench_daily.to_frame("value"))
        joined = pd.concat([monthly, bench_monthly], axis=1, join="inner").dropna()
        if len(joined) < MIN_MONTHS:
            continue
        fund_m = joined.iloc[:, 0]
        bench_m = joined.iloc[:, 1]
        n_total = len(joined)
        row = {
            "scheme_code": code,
            "scheme_name": daily.name and None,  # filled by caller
            "benchmark": key,
            "n_points": n_total,
        }
        fund_rets = fund_m.pct_change().dropna()
        bench_rets = bench_m.pct_change().dropna()
        for w, suffix in WINDOWS.items():
            fc = rolling_cagrs(fund_m, w)
            bc = rolling_cagrs(bench_m, w)
            m = min(len(fc), len(bc))
            if m < 12:      # fewer than a year of rolls: not screenable
                for pre in ("ro", "up", "dc", "tr", "tb", "ex", "sd", "sdb", "exr"):
                    row[f"{pre}{suffix}"] = None
                continue
            fc, bc = fc[-m:], bc[-m:]
            row["ro" + suffix] = float((fc > bc).mean())
            up = bc > 0
            dn = bc < 0
            row["up" + suffix] = float((fc[up] > bc[up]).mean()) if up.sum() >= 3 else None
            row["dc" + suffix] = float((fc[dn] > bc[dn]).mean()) if dn.sum() >= 3 else None
            row["tr" + suffix] = float(fc[-1]) if len(fc) else None
            row["tb" + suffix] = float(bc[-1]) if len(bc) else None
            row["ex" + suffix] = row["tr" + suffix] - row["tb" + suffix]
            row["sd" + suffix] = float(fund_rets.iloc[-w:].std())
            row["sdb" + suffix] = float(bench_rets.iloc[-w:].std())
            row["exr" + suffix] = row["sd" + suffix] - row["sdb" + suffix]
            row["n" + suffix] = m
        out.append(row)
    for r in out:
        r["scheme_name"] = None
    return out


# --------------------------------------------------------------------------
# Batch build
# --------------------------------------------------------------------------
def ensure_benchmarks(needed: set[str]) -> dict[str, pd.Series]:
    """Load/fetch every benchmark series needed by the universe; fall back to
    NIFTY 500 TRI for anything unavailable."""
    bench: dict[str, pd.Series] = {}
    if "crisil6535" in needed:
        needed = set(needed) - {"crisil6535"}
    base = load_nifty500_tri()
    bench["nifty500_tri"] = base
    crisil_equity = base
    for key in sorted(needed - {"nifty500_tri"}):
        s, actual = load_benchmark(key)
        if s is None:
            crisil_equity = base      # hybrid proxy against the fallback too
            bench[key] = base
        else:
            if key == "nifty100_tri":
                crisil_equity = s     # workbook pairs Crisil 65:35 with Nifty 100
            bench[key] = s
    proxy = build_crisil_proxy(crisil_equity)
    if proxy is not None:
        bench["crisil6535"] = proxy
    return bench


def needed_benchmarks() -> set[str]:
    keys: set[str] = set()
    for a, b in BENCHMARK_MAP.values():
        keys.add(a)
        if b:
            keys.add(b)
    return keys


def scheme_benchmarks(category: str, benchmarks: dict[str, pd.Series]) -> dict[str, pd.Series]:
    """The benchmark series relevant to one category: its mapped default plus
    the blended hybrid alternative where one exists."""
    keys = BENCHMARK_MAP.get(category, ("nifty500_tri",))
    keys = [k for k in keys if k]     # None -> single-benchmark categories
    return {k: benchmarks[k] for k in keys if k in benchmarks}


def build(limit: int | None = None, codes: list[int] | None = None) -> pd.DataFrame:
    """Compute metrics for the whole screenable universe and write the Parquet."""
    uni = universe()
    if codes:
        uni = uni[uni["scheme_code"].isin(codes)]
    if limit:
        uni = uni.head(int(limit))
    benchmarks = ensure_benchmarks(needed_benchmarks())
    frames = []
    for i, r in uni.iterrows():
        bench = scheme_benchmarks(r.category, benchmarks)
        rows = scheme_metrics(int(r.scheme_code), bench)
        for row in rows:
            row["scheme_name"] = r.scheme_name
            row["category"] = r.category
        if rows:
            frames.append(pd.DataFrame(rows))
    result = pd.concat(frames, ignore_index=True)
    result.to_parquet(SCREENER_PARQUET, index=False)
    return result


# --------------------------------------------------------------------------
# Query side (the old VBA macros became these SQL predicates)
# --------------------------------------------------------------------------
SCORE_COLS = ["ro", "up", "dc"]


def query(category: str | None, benchmark: str | None, window: str,
          outperf_min: float | None, upside_min: float | None,
          downside_min: float | None, mode: str | None) -> pd.DataFrame:
    """Filtered screener rows; mode='excess' applies the workbook's fourth
    screen (excess return > 0 and excess risk < 0). Any minimum of None means
    "no threshold on this ratio"."""
    if window not in WINDOWS.values():
        raise ValueError(f"window must be one of {list(WINDOWS.values())}")
    con = nav_history._connect()
    where = [f"ro{window} IS NOT NULL"]
    params: list = []
    if category:
        where.append("category = ?")
        params.append(category)
    if benchmark:
        where.append("benchmark = ?")
        params.append(benchmark)
    if mode == "excess":
        where.append(f"ex{window} > 0")
        where.append(f"exr{window} < 0")
    else:
        for col, lo in (("ro", outperf_min), ("up", upside_min), ("dc", downside_min)):
            if lo is not None:
                where.append(f"{col}{window} >= ?")
                params.append(lo)
    sql = (
        "SELECT * FROM read_parquet(?) WHERE " + " AND ".join(where) +
        f" ORDER BY ro{window} DESC LIMIT 200"
    )
    return con.execute(sql, [SCREENER_PARQUET] + params).fetchdf()
