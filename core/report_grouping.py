"""Pure grouping/summary helpers for the Holdings and Tax pages.

The views already compute the raw data (PortfolioView groups, FYTaxSummary
lists, UnrealizedLot lists). These functions only regroup that data into
"group first, then detail" structures with per-group subtotals, the way
Kuvera / INDmoney / TickerTape present a portfolio. No new queries except
the optional starred-asset lookup in ``priority_keys``.
"""
from datetime import date
from decimal import Decimal

from django.db.models import Q

from core.models import Asset
from core.tax.gains import LTCG_EXEMPTION, LTCG_RATE, STCG_RATE, _fy_of

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


def priority_keys(user=None) -> frozenset:
    """Primary keys of starred assets — the join key for tax lots.

    Lots carry ``asset_pk`` (see core.tax.gains), so a starred asset matches
    exactly its own lots, never another asset that shares its name and type.
    With ``user``, only assets that user holds or has transacted are returned.
    """
    qs = Asset.objects.filter(priority=True)
    if user is not None:
        qs = qs.filter(Q(holdings__account__user=user) | Q(transactions__account__user=user))
    return frozenset(qs.values_list("pk", flat=True).distinct())


def lot_owner_ids(lot) -> set:
    """User ids that own a lot (Gain.user_id or UnrealizedLot.user_ids)."""
    ids = set(getattr(lot, "user_ids", ()) or ())
    uid = getattr(lot, "user_id", None)
    if uid is not None:
        ids.add(uid)
    return ids


def is_starred(lot, starred, user_id=None) -> bool:
    """True when the lot's asset is starred and (if given) owned by ``user_id``."""
    pk = getattr(lot, "asset_pk", None)
    if pk is None or pk not in starred:
        return False
    return user_id is None or user_id in lot_owner_ids(lot)


# --------------------------------------------------------------------------- holdings

def _spark_points(values, width: int = 120, height: int = 28) -> str:
    """SVG polyline ``points`` for a value series (empty if nothing to draw)."""
    vals = [float(v) for v in values]
    if len(vals) < 2 or not any(vals):
        return ""
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1.0
    step = width / (len(vals) - 1)
    return " ".join(f"{i * step:.1f},{height - 2 - (v - lo) / span * (height - 4):.1f}"
                    for i, v in enumerate(vals))


def _spark(values, estimated: bool) -> dict | None:
    pts = _spark_points(values)
    if not pts:
        return None
    first, last = float(values[0]), float(values[-1])
    return {"points": pts, "first": first, "last": last,
            "change_pct": (last - first) / first * 100 if first else None,
            "estimated": estimated}


def snapshot_series(snaps, max_points: int = 60, window: int = 180) -> dict:
    """Downsampled snapshot window: dates, totals and per-asset-type values.

    Snapshot ``breakdown`` is keyed by asset type, so per-group sparklines are
    only possible when holdings are grouped by asset class.
    """
    rows = list(snaps or [])[-window:]
    if len(rows) < 2:
        return {}
    step = max(1, -(-len(rows) // max_points))
    picked = rows[::step]
    if picked[-1] is not rows[-1]:
        picked.append(rows[-1])
    return {
        "start": picked[0].date, "end": picked[-1].date,
        "total": [float(r.current) for r in picked],
        "breakdown": [r.breakdown or {} for r in picked],
        "estimated": any(r.estimated for r in picked),
    }


def concentration(holdings, total_current: Decimal, top_n: int = 5) -> dict | None:
    """Top-N positions by value (same asset across accounts counted once)."""
    by_asset: dict = {}
    for h in holdings:
        row = by_asset.setdefault(h.asset_pk, {"name": h.name, "current": ZERO, "accounts": 0})
        row["current"] += h.current
        row["accounts"] += 1
    if not by_asset or not total_current:
        return None
    ranked = sorted(by_asset.values(), key=lambda r: -r["current"])
    for r in ranked:
        r["weight"] = _pct(r["current"], total_current)
    top = ranked[:top_n]
    return {"top": top, "n": len(top), "positions": len(ranked),
            "weight": sum(r["weight"] for r in top), "largest": ranked[0]}


def holdings_report(view, groups, snaps=None) -> dict:
    """Add subtotals and decision cues to the view's holding groups.

    ``groups`` is exactly what the holdings view already passes to the
    template (``view.grouped_holdings`` or ``view.grouped_by(key)``). Each
    returned section keeps every original key and adds: slug, gain, count,
    starred_count, manual_count, rows (holding + % of portfolio + % of
    group), top_gainer / top_loser (by ₹ gain), largest (share of group) and
    spark (value trend from ValueSnapshot, asset-class grouping only).
    ``snaps`` is the user's ValueSnapshot queryset/list, oldest first.
    """
    series = snapshot_series(snaps)
    total_current = getattr(view, "total_current", ZERO) or ZERO
    sections = []
    for i, g in enumerate(groups):
        holdings = g["holdings"]
        invested = g.get("invested", sum((h.invested for h in holdings), ZERO))
        current = g.get("current", sum((h.current for h in holdings), ZERO))
        rows = [{"h": h, "weight": _pct(h.current, total_current), "share": _pct(h.current, current)}
                for h in holdings]
        top_gainer = top_loser = largest = None
        if len(holdings) >= 2:
            best = max(holdings, key=lambda h: h.gain)
            worst = min(holdings, key=lambda h: h.gain)
            top_gainer = best if best.gain > 0 else None
            top_loser = worst if worst.gain < 0 else None
            big = max(rows, key=lambda r: r["h"].current)
            largest = {"name": big["h"].name, "share": big["share"], "asset": big["h"].asset}
        spark = None
        if series and g.get("type"):
            spark = _spark([float(b.get(g["type"], 0) or 0) for b in series["breakdown"]],
                           series["estimated"])
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
            "rows": rows,
            "top_gainer": top_gainer,
            "top_loser": top_loser,
            "largest": largest,
            "spark": spark,
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
        "concentration": concentration(getattr(view, "holdings", []), total_current),
        "spark": _spark(series["total"], series["estimated"]) if series else None,
        "spark_range": (series["start"], series["end"]) if series else None,
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


# One-line tax reminder per category (post-23-Jul-2024 rules). The FY estimate
# on the page applies equity rates to every lot it computes, so categories
# taxed differently say so.
TAX_RULES = {
    "Mutual funds": "Equity funds: STCG 20% under 12 months, LTCG 12.5% above the ₹1.25L exemption. "
                    "Debt funds bought from 1 Apr 2023: slab rate at any holding period.",
    "Stocks & ETFs": "STCG 20% under 12 months · LTCG 12.5% above ₹1.25L per FY (exemption shared with equity funds).",
    "Gold & SGBs": "SGB held to maturity: redemption gain tax-free. Sold early / gold ETF: LT after 12 months at 12.5%; "
                   "physical gold & gold funds: LT after 24 months. Short-term at slab. No ₹1.25L exemption.",
    "Crypto": "Flat 30% + 4% cess at any holding period; losses can't offset other gains; 1% TDS on sale. "
              "The estimate here uses equity rates — treat crypto separately.",
}

# The ₹1.25L Sec 112A exemption covers listed equity and equity funds only.
EXEMPT_POOL = {T.MUTUAL_FUND, T.STOCK}
# Capital losses that can be set off against other capital gains (crypto can't).
SETOFF_OK = set(Asset.EQUITY_LIKE) - {T.CRYPTO}


def _ordered(groups: dict) -> list:
    return [groups[k] for k in CATEGORY_ORDER if k in groups] + \
           [v for k, v in groups.items() if k not in CATEGORY_ORDER]


def realised_by_category(lots, starred, user_id=None) -> dict:
    """Realised Gain lots grouped by asset category, starred assets first."""
    groups: dict[str, dict] = {}
    for lot in lots:
        label = category_of(lot.asset_type)
        g = groups.setdefault(label, {
            "label": label, "slug": "rg-" + _slug(label), "rows": [],
            "cost": ZERO, "proceeds": ZERO, "gain": ZERO,
            "st_gain": ZERO, "lt_gain": ZERO, "starred_count": 0,
            "rule": TAX_RULES.get(label, ""),
        })
        pri = is_starred(lot, starred, user_id)
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


def unrealised_by_category(lots, starred, hide_zero: bool = True, user_id=None) -> dict:
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
            "rule": TAX_RULES.get(label, ""),
        })
        pri = is_starred(lot, starred, user_id)
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


def _tax_with_setoff(st: Decimal, lt: Decimal):
    """Equity CG tax after Indian set-off: short-term loss offsets STCG then
    LTCG; long-term loss offsets LTCG only; exemption applies to net LTCG."""
    if st < 0:
        lt, st = lt + st, ZERO
    tax = max(ZERO, lt - LTCG_EXEMPTION) * LTCG_RATE + max(ZERO, st) * STCG_RATE
    return tax, st, lt


def harvest_now(summaries, unrealized, today: date | None = None) -> dict:
    """"What if I harvest now" for the *current* FY, from lots already computed.

    - Loss harvesting: sell every open lot under water (not crypto — VDA
      losses can't be set off) and see what happens to this FY's tax.
    - Gain harvesting: book long-term equity gains up to the unused ₹1.25L
      exemption (sell + rebuy resets cost; India has no wash-sale rule).
    Widths are % of the ₹1.25L exemption for the stacked headroom bar.
    """
    fy = _fy_of(today or date.today())
    cur = next((s for s in summaries if s and s.fy == fy), None)
    stcg = cur.stcg if cur else ZERO
    ltcg = cur.ltcg if cur else ZERO
    st_loss = sum((l.gain for l in unrealized if l.gain < 0 and not l.is_long_term
                   and l.asset_type in SETOFF_OK), ZERO)
    lt_loss = sum((l.gain for l in unrealized if l.gain < 0 and l.is_long_term
                   and l.asset_type in SETOFF_OK), ZERO)
    crypto_loss = sum((l.gain for l in unrealized if l.gain < 0 and l.asset_type == T.CRYPTO), ZERO)
    lt_gain_pool = sum((l.gain for l in unrealized if l.gain > 0 and l.is_long_term
                        and l.asset_type in EXEMPT_POOL), ZERO)

    tax_now, _, lt_now = _tax_with_setoff(stcg, ltcg)
    tax_after, _, lt_after = _tax_with_setoff(stcg + st_loss, ltcg + lt_loss)
    cap = LTCG_EXEMPTION
    used = min(cap, max(ZERO, lt_now))
    headroom = cap - used
    used_after = min(cap, max(ZERO, lt_after))
    headroom_after = cap - used_after
    book_free = min(headroom, lt_gain_pool)
    book_free_after = min(headroom_after, lt_gain_pool)
    return {
        "fy": fy, "stcg": stcg, "ltcg": ltcg,
        "st_loss": st_loss, "lt_loss": lt_loss, "losses": st_loss + lt_loss,
        "crypto_loss": crypto_loss, "lt_gain_pool": lt_gain_pool,
        "tax_now": tax_now, "tax_after": tax_after, "saving": tax_now - tax_after,
        "carry_forward": -lt_after if lt_after < 0 else ZERO,
        "used": used, "headroom": headroom, "used_after": used_after,
        "headroom_after": headroom_after,
        "book_free": book_free, "book_free_after": book_free_after,
        "used_pct": _pct(used, cap), "book_pct": _pct(book_free, cap),
        "used_after_pct": _pct(used_after, cap), "book_after_pct": _pct(book_free_after, cap),
        "cap": cap,
    }


def tax_report(summaries, summary, unrealized, hide_zero: bool = True,
               starred=None, user=None) -> dict:
    """Everything the grouped Tax page needs, from data the tax view already has.

    Pass ``user`` so starring is per-user: only starred assets the user owns,
    and only lots booked in that user's accounts, are marked.
    """
    if starred is None:
        starred = priority_keys(user)
    user_id = getattr(user, "pk", None)
    return {
        "fy": fy_overview(summaries, summary.fy if summary else None),
        "realised": realised_by_category(summary.lots if summary else [], starred, user_id),
        "unrealised": unrealised_by_category(unrealized, starred, hide_zero, user_id),
        "harvest": harvest_now(summaries, unrealized),
        "current_fy": _fy_of(date.today()),
    }
