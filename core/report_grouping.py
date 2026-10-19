"""Pure grouping/summary helpers for the Holdings and Tax pages.

The views already compute the raw data (PortfolioView groups, FYTaxSummary
lists, UnrealizedLot lists). These functions only regroup that data into
"group first, then detail" structures with per-group subtotals, the way
Kuvera / INDmoney / TickerTape present a portfolio. No new queries except
the optional starred-asset lookup in ``priority_keys``.
"""
from decimal import Decimal

from core.models import Asset

ZERO = Decimal(0)

# Broad categories used on the Tax page (and anywhere lots carry asset_type).
# Ordered the way Indian trackers list them: funds, equity, gold, crypto, debt.
T = Asset.AssetType
CATEGORY_OF = {
    T.MUTUAL_FUND: "Mutual funds",
    T.STOCK: "Stocks & ETFs",
    T.SGB: "Gold & SGBs",
    T.GOLD: "Gold & SGBs",
    T.CRYPTO: "Crypto",
    T.BOND: "Bonds",
    T.FD: "Fixed deposits",
    T.PPF: "Retirement & govt schemes",
    T.EPF: "Retirement & govt schemes",
    T.NPS: "Retirement & govt schemes",
    T.SAVINGS: "Savings",
    T.REAL_ESTATE: "Real estate",
    T.OTHER: "Other",
}
CATEGORY_ORDER = ["Mutual funds", "Stocks & ETFs", "Gold & SGBs", "Crypto", "Bonds",
                  "Fixed deposits", "Retirement & govt schemes", "Savings",
                  "Real estate", "Other"]


def category_of(asset_type: str) -> str:
    return CATEGORY_OF.get(asset_type, "Other")


def _pct(num: Decimal, den: Decimal) -> float:
    return float(num / den * 100) if den else 0.0


def _slug(label: str) -> str:
    return "-".join("".join(c if c.isalnum() else " " for c in label.lower()).split())


def priority_keys() -> set:
    """(asset_type, name) pairs of starred assets.

    Tax lots carry only name + type (no asset pk), so this is the join key.
    """
    return set(Asset.objects.filter(priority=True).values_list("asset_type", "name"))


# --------------------------------------------------------------------------- holdings

def holdings_report(view, groups) -> dict:
    """Add subtotals to the view's holding groups (any grouping mode).

    ``groups`` is exactly what the holdings view already passes to the
    template (``view.grouped_holdings`` or ``view.grouped_by(key)``). Each
    returned section keeps every original key and adds: slug, gain, count,
    starred_count, manual_count.
    """
    sections = []
    for i, g in enumerate(groups):
        holdings = g["holdings"]
        invested = g.get("invested", sum((h.invested for h in holdings), ZERO))
        current = g.get("current", sum((h.current for h in holdings), ZERO))
        sections.append({
            **g,
            "slug": f"grp-{i}",  # matches template anchor grp-{{ forloop.counter0 }}
            "invested": invested,
            "current": current,
            "gain": current - invested,
            "gain_pct": _pct(current - invested, invested),
            "count": len(holdings),
            "starred_count": sum(1 for h in holdings if h.priority),
            "manual_count": sum(1 for h in holdings if h.manual_value),
        })
    invested = sum((s["invested"] for s in sections), ZERO)
    current = sum((s["current"] for s in sections), ZERO)
    return {
        "sections": sections,
        "totals": {
            "invested": invested, "current": current,
            "gain": current - invested, "gain_pct": _pct(current - invested, invested),
            "count": sum(s["count"] for s in sections),
            "starred_count": sum(s["starred_count"] for s in sections),
        },
    }


# --------------------------------------------------------------------------- tax

def fy_overview(summaries, selected_fy) -> dict:
    """One row per financial year (STCG / LTCG / tax / lots) plus all-years totals."""
    rows = [{
        "fy": s.fy, "stcg": s.stcg, "ltcg": s.ltcg, "tax": s.tax,
        "lots": len(s.lots), "selected": s.fy == selected_fy,
    } for s in summaries]
    return {
        "rows": rows,
        "totals": {
            "stcg": sum((r["stcg"] for r in rows), ZERO),
            "ltcg": sum((r["ltcg"] for r in rows), ZERO),
            "tax": sum((r["tax"] for r in rows), ZERO),
            "lots": sum(r["lots"] for r in rows),
        },
    }


def _ordered(groups: dict) -> list:
    return [groups[k] for k in CATEGORY_ORDER if k in groups] + \
           [v for k, v in groups.items() if k not in CATEGORY_ORDER]


def realised_by_category(lots, starred: set) -> dict:
    """Realised Gain lots grouped by asset category, starred assets first."""
    groups: dict[str, dict] = {}
    for lot in lots:
        label = category_of(lot.asset_type)
        g = groups.setdefault(label, {
            "label": label, "slug": "rg-" + _slug(label), "rows": [],
            "cost": ZERO, "proceeds": ZERO, "gain": ZERO,
            "st_gain": ZERO, "lt_gain": ZERO, "starred_count": 0,
        })
        pri = (lot.asset_type, lot.asset_name) in starred
        g["rows"].append({"lot": lot, "priority": pri})
        g["cost"] += lot.cost
        g["proceeds"] += lot.proceeds
        g["gain"] += lot.gain
        g["st_gain" if not lot.is_long_term else "lt_gain"] += lot.gain
        g["starred_count"] += pri
    for g in groups.values():
        g["rows"].sort(key=lambda r: (not r["priority"], r["lot"].sell_date, r["lot"].asset_name))
        g["count"] = len(g["rows"])
    out = _ordered(groups)
    return {
        "groups": out,
        "totals": {k: sum((g[k] for g in out), ZERO)
                   for k in ("cost", "proceeds", "gain", "st_gain", "lt_gain")},
    }


def unrealised_by_category(lots, starred: set, hide_zero: bool = True) -> dict:
    """Open lots grouped by asset category with ST/LT split and harvestable losses.

    Subtotals always cover every lot; ``hide_zero`` only drops ₹0-gain rows
    from the visible detail (count reported as ``hidden``).
    """
    groups: dict[str, dict] = {}
    starred_tot = {"count": 0, "cost": ZERO, "current": ZERO, "gain": ZERO}
    for lot in lots:
        label = category_of(lot.asset_type)
        g = groups.setdefault(label, {
            "label": label, "slug": "ug-" + _slug(label), "rows": [],
            "cost": ZERO, "current": ZERO, "gain": ZERO,
            "st_gain": ZERO, "lt_gain": ZERO, "losses": ZERO,
            "count": 0, "hidden": 0, "starred_count": 0,
        })
        pri = (lot.asset_type, lot.asset_name) in starred
        g["count"] += 1
        g["cost"] += lot.cost
        g["current"] += lot.current
        g["gain"] += lot.gain
        g["lt_gain" if lot.is_long_term else "st_gain"] += lot.gain
        if lot.gain < 0:
            g["losses"] += lot.gain
        if pri:
            g["starred_count"] += 1
            starred_tot["count"] += 1
            starred_tot["cost"] += lot.cost
            starred_tot["current"] += lot.current
            starred_tot["gain"] += lot.gain
        if hide_zero and lot.gain == 0:
            g["hidden"] += 1
        else:
            g["rows"].append({"lot": lot, "priority": pri})
    for g in groups.values():
        # starred first, then biggest absolute gain/loss — what you'd act on
        g["rows"].sort(key=lambda r: (not r["priority"], -abs(r["lot"].gain)))
    out = _ordered(groups)
    totals = {k: sum((g[k] for g in out), ZERO)
              for k in ("cost", "current", "gain", "st_gain", "lt_gain", "losses")}
    totals["count"] = sum(g["count"] for g in out)
    totals["hidden"] = sum(g["hidden"] for g in out)
    return {"groups": out, "totals": totals, "starred": starred_tot}


def tax_report(summaries, summary, unrealized, hide_zero: bool = True, starred: set | None = None) -> dict:
    """Everything the grouped Tax page needs, from data the tax view already has."""
    if starred is None:
        starred = priority_keys()
    return {
        "fy": fy_overview(summaries, summary.fy if summary else None),
        "realised": realised_by_category(summary.lots if summary else [], starred),
        "unrealised": unrealised_by_category(unrealized, starred, hide_zero),
    }
