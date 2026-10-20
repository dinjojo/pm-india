"""Portfolio value history: daily snapshots + backfill from historical NAVs.

Forward snapshots (recorded after every import/add) are exact. Backfilled
month-end points are reconstructed: mutual funds from real historical NAVs
(the local AMFI master Parquet, full history), everything else from
cumulative deposits — the UI labels these estimates.
"""
import json
from bisect import bisect_right
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import requests

from core.models import Asset, Transaction, ValueSnapshot
from core import nav_history as nav_history_mod
from core.portfolio import portfolio_view

NAV_CACHE_DIR = Path("data/nav_cache")
MF_SOURCES = {"mf"}  # asset types with a real historical price feed


def nav_history(identifier: str) -> dict[date, Decimal]:
    """ISIN -> {date: NAV} using mfapi.in, cached on disk (one fetch per scheme)."""
    if not identifier:
        return {}
    NAV_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache = NAV_CACHE_DIR / f"{identifier}.json"
    if cache.exists():
        raw = json.loads(cache.read_text())
    else:
        try:
            search = requests.get(f"https://api.mfapi.in/mf/search?q={identifier}", timeout=15)
            search.raise_for_status()
            matches = search.json()
            if not matches:
                cache.write_text("{}")
                return {}
            code = matches[0]["schemeCode"]
            hist = requests.get(f"https://api.mfapi.in/mf/{code}", timeout=15)
            hist.raise_for_status()
            raw = {
                "nav": {
                    item["date"]: item["nav"]
                    for item in hist.json().get("data", [])
                }
            }
            cache.write_text(json.dumps(raw))
        except Exception:
            return {}  # no network / API down: caller falls back to invested
    out = {}
    for d, nav in raw.get("nav", {}).items():
        try:
            day = date(*reversed([int(x) for x in d.split("-")]))  # "DD-MM-YYYY"
            out[day] = Decimal(nav)
        except Exception:
            continue
    return out


def _month_ends_since(first: date) -> list[date]:
    """Month-end dates from the month of `first` through last month, sorted."""
    out, d = [], first.replace(day=1)
    today = date.today()
    while d <= today:
        last = (d.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
        if last <= today and last >= first:  # never create future-dated points
            out.append(last)
        d = (last + timedelta(days=1)).replace(day=1)
    return sorted(out)


def _asset_nav_history(asset) -> dict[date, Decimal]:
    """{date: NAV} for one asset, preferring the local AMFI master Parquet
    (full history via the asset's AMFI scheme code) with mfapi.in as fallback."""
    code = (asset.metadata or {}).get("scheme_code")
    if code:
        try:
            series = nav_history_mod.local_nav_history(int(code))
        except Exception:
            series = []
        if series:
            out = {}
            for d, nav in series:
                try:
                    out[date.fromisoformat(d)] = Decimal(str(nav))
                except Exception:
                    continue
            return out
    return nav_history(asset.identifier)


def backfill_snapshots(user, months: int | None = None) -> int:
    """Rebuild month-end history for the trend chart from transactions and
    historical NAVs. months=None covers the full history since the user's
    earliest transaction. Returns count written."""
    rows = list(
        Transaction.objects.filter(account__user=user)
        .order_by("date", "id")
        .values_list("asset_id", "account_id", "kind", "date", "quantity", "amount")
    )
    if not rows:
        return 0
    positions = {}  # (asset_id, account_id) -> cumulative state
    for asset_id, account_id, kind, tx_date, quantity, amount in rows:
        key = (asset_id, account_id)
        pos = positions.setdefault(key, {"txs": [], "qty": Decimal(0), "amt": Decimal(0)})
        pos["txs"].append((tx_date, kind, quantity, amount))
    first_day = min(r[3] for r in rows)
    ends = _month_ends_since(first_day)
    if months is not None:
        ends = ends[-months:]

    assets = {a.id: a for a in Asset.objects.filter(id__in={k[0] for k in positions})}
    navs = {a.id: _asset_nav_history(a)
            for a in assets.values() if a.asset_type in MF_SOURCES}
    nav_days = {a_id: sorted(hist) for a_id, hist in navs.items()}

    written = 0
    for day in ends:
        invested_total, current_total = Decimal(0), Decimal(0)
        breakdown: dict[str, float] = {}
        for key, pos in positions.items():
            asset_id, account_id = key
            asset = assets.get(asset_id)
            if asset is None:
                continue
            # Advance the cumulative position through this month-end.
            while pos["txs"] and pos["txs"][0][0] <= day:
                tx_date, kind, quantity, amount = pos["txs"].pop(0)
                sign = 1 if kind in (Transaction.Kind.BUY, Transaction.Kind.SWITCH_IN) else -1
                pos["qty"] += sign * quantity
                pos["amt"] += sign * amount
            if not pos["amt"]:
                continue
            invested_total += pos["amt"]
            hist = navs.get(asset_id)
            if asset.asset_type in MF_SOURCES and hist and pos["qty"]:
                days = nav_days[asset_id]
                i = bisect_right(days, day)
                current = pos["qty"] * hist[days[i - 1]] if i else pos["amt"]
            else:
                current = pos["amt"]  # estimate: balance-type / no feed / pre-history
            current_total += current
            breakdown[asset.asset_type] = float(breakdown.get(asset.asset_type, 0)) + float(current)
        ValueSnapshot.objects.update_or_create(
            user=user, date=day,
            defaults={"invested": invested_total, "current": current_total,
                      "breakdown": breakdown, "estimated": True},
        )
        written += 1
    return written


def record_snapshot(user) -> ValueSnapshot:
    """Snapshot today's portfolio at its real current values (exact, not estimated)."""
    pv = portfolio_view(user)
    breakdown: dict[str, float] = {}
    for h in pv.holdings:
        breakdown[h.asset_type] = float(breakdown.get(h.asset_type, 0)) + float(h.current)
    snap, _ = ValueSnapshot.objects.update_or_create(
        user=user, date=date.today(),
        defaults={"invested": pv.total_invested, "current": pv.total_current,
                  "breakdown": breakdown, "estimated": False},
    )
    return snap
