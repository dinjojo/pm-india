"""Decision-framed context for the dashboard: recent movement, attention items,
goal status and a one-sentence narrative.

Everything here is derived from existing models (holdings, price points,
value snapshots, goal plans, open tax lots). Nothing is estimated beyond what
those sources already provide; when a source is empty the matching key is
empty/None and the template hides the section.
"""
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal

from core.goals import one_time_goal, recurring_goal, retirement_plan, sip_affordability
from core.models import Holding, PricePoint, RetirementSettings
from core.templatetags.inr import inr_short

STALE_PRICE_DAYS = 7      # a feed price older than a week is flagged
TAX_WINDOW_DAYS = 60      # equity lots turning long-term within this window
MOVEMENT_DAYS = 30        # "how did it move recently"
GOALS_SHOWN = 3


# ---------------------------------------------------------------------------
# Pure helpers (no DB) — unit tested in core/tests_dashboard_context.py
# ---------------------------------------------------------------------------

def snapshot_movement(snaps, days: int = MOVEMENT_DAYS) -> dict | None:
    """Change in portfolio value over roughly the last `days`.

    `snaps` is a date-ordered sequence of objects with .date, .invested,
    .current and .estimated (ValueSnapshot rows). Only exact snapshots are
    used: backfilled ones value non-MF assets at cost, so a "market move"
    measured from them would be an artifact. The base point is the last exact
    snapshot on or before latest.date − days, else the earliest exact one. The
    change is split into new money (Δ invested) and market movement (the rest).
    """
    snaps = [s for s in snaps if not s.estimated]
    if len(snaps) < 2:
        return None
    latest = snaps[-1]
    cutoff = latest.date - timedelta(days=days)
    base = snaps[0]
    for s in snaps[:-1]:
        if s.date <= cutoff:
            base = s
        else:
            break
    change = Decimal(latest.current) - Decimal(base.current)
    flows = Decimal(latest.invested) - Decimal(base.invested)
    market = change - flows
    return {
        "from_date": base.date,
        "to_date": latest.date,
        "days": (latest.date - base.date).days,
        "change": change,
        "change_abs": abs(change),
        "change_pct": float(change / Decimal(base.current) * 100) if base.current else None,
        "flows": flows,
        "flows_abs": abs(flows),
        "market": market,
        "market_abs": abs(market),
        "market_pct": float(market / Decimal(base.current) * 100) if base.current else None,
    }


def goal_years_away(plan) -> int | None:
    """Years until the goal's money is needed (start of spending)."""
    if plan.kind == "one_time":
        return plan.years_to_goal
    if plan.kind == "recurring":
        return plan.years_until_start
    if plan.current_age is not None and plan.retire_age is not None:
        return max(0, plan.retire_age - plan.current_age)
    return None


def classify_goal(kind: str, *, net_corpus: float, on_track: bool | None,
                  affordability: dict | None) -> str:
    """'funded' | 'on_track' | 'stretch' | 'off_track'.

    Retirement uses the engine's own on_track flag. Other goals are 'funded'
    when nothing more is needed, 'off_track' when the required SIP is an
    aggressive share of take-home pay, 'stretch' in the 25–40% band, and
    otherwise 'on_track' (a SIP is needed but affordable or unknown).
    """
    if kind == "retirement":
        if on_track:
            return "funded" if net_corpus <= 0 else "on_track"
        return "off_track"
    if net_corpus <= 0:
        return "funded"
    band = (affordability or {}).get("band")
    if band == "aggressive":
        return "off_track"
    if band == "stretch":
        return "stretch"
    return "on_track"


def smart_narrative(*, total_current, total_invested, holdings_count: int,
                    movement: dict | None, attention_count: int) -> str:
    """One plain-language sentence about where the portfolio stands."""
    if not holdings_count:
        return "No holdings yet — add an investment or import a CAS statement to get started."
    gain = Decimal(total_current) - Decimal(total_invested)
    noun = "holding is" if holdings_count == 1 else "holdings are"
    parts = [f"Your {holdings_count} {noun} worth {inr_short(total_current)}"]
    if total_invested:
        pct = gain / Decimal(total_invested) * 100
        word = "up" if gain >= 0 else "down"
        parts.append(f", {word} {inr_short(gain)} ({abs(pct):.1f}%) on what you put in")
    if movement and movement["days"]:
        m, n = movement["market"], movement["days"]
        span = f"over the last {n} day{'s' if n != 1 else ''}"
        if m == 0:
            parts.append(f"; markets were flat {span}")
        else:
            word = "added" if m > 0 else "took off"
            parts.append(f"; markets {word} {inr_short(m)} {span}")
    if attention_count:
        parts.append(f" — {attention_count} item{'s' if attention_count != 1 else ''} need"
                     f"{'s' if attention_count == 1 else ''} a look below.")
    else:
        parts.append(" — nothing needs attention right now.")
    return "".join(parts)


# ---------------------------------------------------------------------------
# DB-backed builders
# ---------------------------------------------------------------------------

def value_gaps(user, today: date | None = None) -> dict:
    """Holdings whose displayed value is a fallback or out of date.

    missing: no price feed and no manual value — shown at cost.
    stale:   priced from a feed whose latest price is older than STALE_PRICE_DAYS.
    (Holding.updated_at is touched by every holdings refresh, so it cannot tell
    when a manual value was last entered; manual values are not flagged stale.)
    """
    today = today or date.today()
    holdings = list(Holding.objects.select_related("asset").filter(account__user=user))
    idents = {h.asset.identifier for h in holdings if h.asset.identifier}
    prices = {(p.asset_type, p.identifier): p
              for p in PricePoint.objects.filter(identifier__in=idents)}
    missing, stale = [], []
    for h in holdings:
        pp = prices.get((h.asset.asset_type, h.asset.identifier)) if h.asset.identifier else None
        if pp is None:
            if h.current_value is None:
                missing.append({"name": h.asset.name, "invested": h.invested, "asset": h.asset})
        elif (today - pp.as_of).days > STALE_PRICE_DAYS:
            stale.append({"name": h.asset.name, "as_of": pp.as_of,
                          "age_days": (today - pp.as_of).days})
    missing.sort(key=lambda r: -r["invested"])
    stale.sort(key=lambda r: -r["age_days"])
    return {"missing": missing, "stale": stale}


def _goal_numbers(plan, a) -> tuple[float, float, bool | None]:
    """(net_corpus, monthly_sip, on_track) using the same engine inputs as the goal page."""
    common = dict(step_up_pct=float(plan.step_up_pct),
                  current_equity=float(plan.current_equity),
                  current_taxable_fi=float(plan.current_taxable_fi),
                  current_taxfree_fi=float(plan.current_taxfree_fi))
    infl = float(plan.inflation_override) if plan.inflation_override else None
    if plan.kind == "one_time":
        r = one_time_goal(a=a, cost_today=float(plan.cost_today or 0),
                          years_to_goal=float(plan.years_to_goal or 0),
                          years_to_start=float(plan.years_to_start),
                          lumpsum_expected=float(plan.lumpsum_expected),
                          flexible=plan.flexible, inflation_override=infl, **common)
        return r.net_corpus, r.monthly_sip, None
    if plan.kind == "recurring":
        r = recurring_goal(a=a, annual_cost_today=float(plan.annual_cost or 0),
                           years_until_start=float(plan.years_until_start or 0),
                           years_duration=float(plan.years_duration or 0),
                           flexible=plan.flexible, inflation_override=infl, **common)
        return r.net_corpus, r.monthly_sip, None
    if infl:
        a = replace(a, inflation=infl, inflation_retirement=infl)
    r = retirement_plan(a=a, current_age=plan.current_age or 0, retire_age=plan.retire_age or 0,
                        monthly_expense=float(plan.monthly_expense or 0),
                        annual_expense=float(plan.annual_expense or 0),
                        lumpsum_at_retirement=float(plan.lumpsum_expected),
                        income_streams=plan.income_streams(),
                        floor_pct=float(plan.pension_floor_pct or 0), **common)
    return r.net_corpus, r.monthly_sip, r.on_track


def goal_statuses(user) -> list[dict]:
    """Goals ordered off-track first, then nearest deadline. Goals the engine
    cannot evaluate (incomplete inputs) are skipped rather than guessed."""
    plans = list(user.goal_plans.all())
    if not plans:
        return []
    a = RetirementSettings.load().to_assumptions()
    out = []
    for p in plans:
        try:
            net, sip, on_track = _goal_numbers(p, a)
        except Exception:  # incomplete plan inputs — the goal page will show why
            continue
        afford = sip_affordability(sip, p.monthly_take_home)
        status = classify_goal(p.kind, net_corpus=net, on_track=on_track, affordability=afford)
        out.append({"pk": p.pk, "name": p.name, "kind_label": p.get_kind_display(),
                    "years_away": goal_years_away(p), "status": status,
                    "net_corpus": net, "monthly_sip": sip, "affordability": afford})
    rank = {"off_track": 0, "stretch": 1, "on_track": 2, "funded": 3}
    out.sort(key=lambda g: (rank[g["status"]],
                            g["years_away"] if g["years_away"] is not None else 999))
    return out


def tax_events(today: date | None = None, window: int = TAX_WINDOW_DAYS) -> list[dict]:
    """Open equity lots in profit that turn long-term within `window` days —
    selling before that date is taxed as STCG (20%) instead of LTCG (12.5%)."""
    from core.tax.gains import unrealized_lots

    today = today or date.today()
    out = []
    for lot in unrealized_lots():
        if lot.is_long_term or lot.gain <= 0:
            continue
        days = (lot.long_term_on - today).days
        if 0 <= days <= window:
            out.append({"name": lot.asset_name, "long_term_on": lot.long_term_on,
                        "days": days, "gain": lot.gain, "asset": lot.asset})
    return sorted(out, key=lambda e: e["days"])


def _safe_xirr_pct(pv) -> float | None:
    """PortfolioView.xirr returns a fraction (0.12); the KPI shows a percentage."""
    try:
        x = pv.xirr
    except Exception:
        return None
    if x is None or x != x or abs(x) > 100:  # None, NaN, or a meaningless blow-up
        return None
    return x * 100


def build_dashboard_context(user, pv, snaps=None) -> dict:
    """Everything the decision-framed dashboard needs beyond `pv`, under `dash`."""
    if snaps is None:
        snaps = user.value_snapshots.order_by("date")
    movement = snapshot_movement(snaps)
    gaps = value_gaps(user)
    goals = goal_statuses(user)
    taxes = tax_events()
    priority = [h for h in pv.holdings if h.priority]

    attention_goals = [g for g in goals if g["status"] in ("off_track", "stretch")]
    attention_count = (len(attention_goals) + (1 if gaps["missing"] else 0)
                       + (1 if gaps["stale"] else 0) + len(taxes))
    return {"dash": {
        "xirr_pct": _safe_xirr_pct(pv),
        "gain_abs": abs(pv.total_gain),
        "gain_pct_abs": abs(pv.total_gain_pct),
        "movement": movement,
        "missing": gaps["missing"],
        "stale": gaps["stale"],
        "goals": goals[:GOALS_SHOWN],
        "goals_total": len(goals),
        "tax_events": taxes,
        "priority": priority,
        "attention_count": attention_count,
        "narrative": smart_narrative(total_current=pv.total_current,
                                     total_invested=pv.total_invested,
                                     holdings_count=len(pv.holdings),
                                     movement=movement,
                                     attention_count=attention_count),
    }}
