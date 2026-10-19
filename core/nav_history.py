"""Local NAV history from the tigzig full-dataset Parquet (tier A).

The complete AMFI daily NAV history — 37.6M rows, 38,289 schemes including
matured ones, April 2006 onward — lives at data/amfi_nav_master.parquet and is
queried in place with DuckDB (HTTP-free, no rate limits, survivorship-complete).
Refresh weekly by re-downloading:

    curl -L -o data/amfi_nav_master.parquet \\
         https://api.tigzig.com/mf/v1/download/amfi_nav_master.parquet

Tier B (live per-scheme NAVs, holdings refresh) remains mfapi.in via
core/snapshots.py and core/funds.py, which fall back to the network when the
local file lacks a scheme.
"""
import os
from datetime import date

import duckdb

BASE_DIR = os.path.dirname(os.path.dirname(__file__))
NAV_PARQUET = os.path.join(BASE_DIR, "data", "amfi_nav_master.parquet")

_conn = None


def _parquet_exists() -> bool:
    return os.path.exists(NAV_PARQUET)


def _connect():
    global _conn
    if _conn is None:
        _conn = duckdb.connect(database=":memory:")
    return _conn


def local_nav_history(code: int) -> list[tuple[str, float]]:
    """Ascending [(iso-date, nav)] for one scheme from the local Parquet, [] if
    the file or scheme is absent (caller falls back to the network)."""
    if not _parquet_exists():
        return []
    rows = _connect().execute(
        "SELECT date, nav FROM read_parquet(?) "
        "WHERE scheme_code = ? AND nav IS NOT NULL ORDER BY date",
        [NAV_PARQUET, int(code)],
    ).fetchall()
    return [(d.isoformat() if isinstance(d, date) else str(d), float(n)) for d, n in rows]


def local_scheme_meta(code: int) -> dict:
    """Scheme name + ISIN from the local Parquet, {} when unknown."""
    if not _parquet_exists():
        return {}
    rows = _connect().execute(
        "SELECT scheme_name, isin FROM read_parquet(?) WHERE scheme_code = ? LIMIT 1",
        [NAV_PARQUET, int(code)],
    ).fetchall()
    if not rows:
        return {}
    name, isin = rows[0]
    return {"scheme_name": name, "isin_growth": isin or ""}


def local_scheme_names(q: str, limit: int = 25) -> list[dict]:
    """Scheme search across the local universe (live + matured)."""
    if not _parquet_exists() or len(q.strip()) < 3:
        return []
    rows = _connect().execute(
        "SELECT scheme_code, scheme_name FROM read_parquet(?) "
        "WHERE lower(scheme_name) LIKE ? ORDER BY scheme_name LIMIT ?",
        [NAV_PARQUET, f"%{q.strip().lower()}%", int(limit)],
    ).fetchall()
    return [{"code": c, "name": n} for c, n in rows]
