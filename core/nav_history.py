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
import tempfile
from datetime import date

import duckdb
import requests

BASE_DIR = os.path.dirname(os.path.dirname(__file__))
NAV_PARQUET = os.path.join(BASE_DIR, "data", "amfi_nav_master.parquet")
NAV_MASTER_URL = "https://api.tigzig.com/mf/v1/download/amfi_nav_master.parquet"

_conn = None


def refresh_nav_master():
    """Stream and validate a new master before replacing the working copy."""
    os.makedirs(os.path.dirname(NAV_PARQUET), exist_ok=True)
    path = None
    try:
        with tempfile.NamedTemporaryFile(dir=os.path.dirname(NAV_PARQUET),
                                         suffix=".parquet", delete=False) as tmp:
            path = tmp.name
            with requests.get(NAV_MASTER_URL, stream=True, timeout=(15, 180)) as response:
                response.raise_for_status()
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    tmp.write(chunk)
        with duckdb.connect() as con:
            newest = con.execute(
                "SELECT max(date) FROM read_parquet(?) WHERE nav > 0 AND scheme_code IS NOT NULL",
                [path],
            ).fetchone()[0]
        if newest is None:
            raise ValueError("Downloaded NAV master contains no usable NAVs")
        os.replace(path, NAV_PARQUET)
        return newest
    finally:
        if path and os.path.exists(path):
            os.unlink(path)


def latest_nav_date():
    if not _parquet_exists():
        return None
    return _connect().execute("SELECT max(CAST(date AS DATE)) FROM read_parquet(?)",
                              [NAV_PARQUET]).fetchone()[0]


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
