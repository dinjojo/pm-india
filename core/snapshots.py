"""Portfolio value history: daily snapshots + backfill from historical NAVs.

Forward snapshots (daily job) are exact. Backfilled month-end points are
reconstructed: mutual funds from real historical NAVs (mfapi.in, free),
everything else from cumulative deposits — the UI labels these estimates.
"""
import json
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import requests

from core.models import Asset, Transaction, ValueSnapshot
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


def _month_ends(months: int) -> list[date]:
    out, d = [], date.today().replace(day=1)
    for _ in range(months):
        last = (d.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
        if last <= date.today():  # never create future-dated points
            out.append(last)
        d = (d - timedelta(days=1)).replace(day=1)
    return sorted(out)


def _position_at(asset_id: int, account_id: int, day: date) -> tuple[Decimal, Decimal]:
    """(quantity, invested) for a holding as of `day`, from transactions."""
    agg = {"qty": Decimal(0), "amt": Decimal(0)}
    for tx in Transaction.objects.filter(asset_id=asset_id, account_id=account_id, date__lte=day):
        sign = 1 if tx.kind in (Transaction.Kind.BUY, Transaction.Kind.SWITCH_IN) else -1
        agg["qty"] += sign * tx.quantity
        agg["amt"] += sign * tx.amount
    return agg["qty"], agg["amt"]


def backfill_snapshots(user, months: int = 12) -> int:
    """Rebuild month-end history for the trend chart. Returns count written."""
    holdings = list(
        Transaction.objects.filter(account__user=user)
        .values_list("asset_id", "account_id").distinct()
    )
    assets = {a.id: a for a in Asset.objects.filter(
        id__in=[asset_id for asset_id, _ in holdings])}
    navs = {a.id: nav_history(a.identifier)
            for a in assets.values() if a.asset_type in MF_SOURCES}

    written = 0
    for day in _month_ends(months):
        invested_total, current_total = Decimal(0), Decimal(0)
        breakdown: dict[str, float] = {}
        for asset_id, account_id in holdings:
            asset = assets.get(asset_id)
            if asset is None:
                continue
            qty, invested = _position_at(asset_id, account_id, day)
            if not invested:
                continue
            invested_total += invested
            hist = navs.get(asset_id)
            if asset.asset_type in MF_SOURCES and hist and qty:
                navs_le = [d for d in hist if d <= day]
                current = qty * hist[max(navs_le)] if navs_le else invested
            else:
                current = invested  # estimate: balance-type / no feed / pre-history
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
