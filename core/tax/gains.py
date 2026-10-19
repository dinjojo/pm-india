"""FY capital-gains computation under post-July-2024 Indian rules.

- Listed equity / equity MF: STCG 20% (< 12 months), LTCG 12.5% on gains
  above ₹1.25L per FY (> 12 months).
- Other assets: slab rate (reported, not computed).
- Grandfathering of pre-2018-01-31 equity buys (FMV as on 31-1-2018) is a
  planned addition; it needs the 31-Jan-2018 price series.
"""
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from django.db.models import F

from core.models import Asset, Holding, Transaction

LTCG_EXEMPTION = Decimal("125000")
LTCG_RATE = Decimal("0.125")
STCG_RATE = Decimal("0.20")
LONG_TERM_DAYS = 365


@dataclass
class Gain:
    asset_name: str
    asset_type: str
    buy_date: date
    sell_date: date
    quantity: Decimal
    cost: Decimal
    proceeds: Decimal
    holding_period_days: int
    # Identity of the lot's source, so UI joins (e.g. starred assets) match the
    # real Asset row and owner, not a (type, name) string pair that can collide.
    asset_pk: int | None = None
    isin: str = ""
    user_id: int | None = None  # owner of the account the sell was booked in

    @property
    def is_long_term(self) -> bool:
        return self.holding_period_days > LONG_TERM_DAYS

    @property
    def gain(self) -> Decimal:
        return self.proceeds - self.cost


@dataclass
class FYTaxSummary:
    fy: str
    lots: list = field(default_factory=list)

    @property
    def stcg(self) -> Decimal:
        return sum((g.gain for g in self.lots if not g.is_long_term), Decimal(0))

    @property
    def ltcg(self) -> Decimal:
        return sum((g.gain for g in self.lots if g.is_long_term), Decimal(0))

    @property
    def ltcg_taxable(self) -> Decimal:
        return max(Decimal(0), self.ltcg - LTCG_EXEMPTION)

    @property
    def tax(self) -> Decimal:
        return (self.ltcg_taxable * LTCG_RATE) + (max(Decimal(0), self.stcg) * STCG_RATE)


def _fy_of(d: date) -> str:
    """Indian financial year label, e.g. 2024-25 (April-March)."""
    start_year = d.year if d.month >= 4 else d.year - 1
    return f"{start_year}-{(start_year + 1) % 100:02d}"


def realized_gains(account_ids=None, fy: str | None = None) -> list[FYTaxSummary]:
    """Match sells against buys FIFO per asset and bucket by financial year."""
    qs = (
        Transaction.objects.filter(asset__asset_type__in=Asset.EQUITY_LIKE)
        .filter(kind__in=[Transaction.Kind.BUY, Transaction.Kind.SELL])
        .select_related("asset", "account")
        .order_by("date", "id")
    )
    if account_ids:
        qs = qs.filter(account_id__in=account_ids)

    lots: dict[int, list] = defaultdict(list)  # asset_id -> FIFO buy lots
    gains: dict[str, list] = defaultdict(list)
    for tx in qs:
        if tx.kind == Transaction.Kind.BUY:
            lots[tx.asset_id].append(tx)
            continue
        remaining = tx.quantity
        while remaining > 0 and lots[tx.asset_id]:
            buy = lots[tx.asset_id][0]
            take = min(remaining, buy.quantity)
            cost = (buy.price * take).quantize(Decimal("0.01"))
            proceeds = (tx.price * take).quantize(Decimal("0.01"))
            g = Gain(
                asset_name=tx.asset.name,
                asset_type=tx.asset.asset_type,
                buy_date=buy.date,
                sell_date=tx.date,
                quantity=take,
                cost=cost,
                proceeds=proceeds,
                holding_period_days=(tx.date - buy.date).days,
                asset_pk=tx.asset_id,
                isin=tx.asset.isin,
                user_id=tx.account.user_id,
            )
            if fy is None or _fy_of(tx.date) == fy:
                gains[_fy_of(tx.date)].append(g)
            remaining -= take
            if take == buy.quantity:
                lots[tx.asset_id].pop(0)
            else:
                buy.quantity -= take

    return [FYTaxSummary(fy=fy_, lots=sorted(ls, key=lambda g: g.sell_date))
            for fy_, ls in sorted(gains.items())]


def harvesting_headroom(summary: FYTaxSummary) -> Decimal:
    """How much LTCG room is left this FY before the ₹1.25L exemption is used."""
    return max(Decimal(0), LTCG_EXEMPTION - max(Decimal(0), summary.ltcg))


@dataclass
class UnrealizedLot:
    asset_name: str
    asset_type: str
    buy_date: date
    quantity: Decimal
    cost: Decimal
    current: Decimal
    # Identity (see Gain): the open lot aggregates every remaining buy of one
    # Asset, so it carries that asset's pk and the owners of those buys.
    asset_pk: int | None = None
    isin: str = ""
    user_ids: frozenset = frozenset()

    @property
    def gain(self) -> Decimal:
        return self.current - self.cost

    @property
    def is_long_term(self) -> bool:
        return (date.today() - self.buy_date).days > LONG_TERM_DAYS

    @property
    def long_term_on(self) -> date:
        return date.fromordinal(self.buy_date.toordinal() + LONG_TERM_DAYS + 1)


def unrealized_lots():
    """Open FIFO lots for equity-like assets with their current value.

    Current value uses the price feed when one exists, else cost (no estimate
    invented) — the UI says so.
    """
    from core.models import PricePoint

    qs = (
        Transaction.objects.filter(asset__asset_type__in=Asset.EQUITY_LIKE)
        .filter(kind__in=[Transaction.Kind.BUY, Transaction.Kind.SELL])
        .select_related("asset", "account")
        .order_by("date", "id")
    )
    lots: dict[int, list] = defaultdict(list)
    for tx in qs:
        if tx.kind == Transaction.Kind.BUY:
            lots[tx.asset_id].append(tx)
            continue
        remaining = tx.quantity
        while remaining > 0 and lots[tx.asset_id]:
            buy = lots[tx.asset_id][0]
            take = min(remaining, buy.quantity)
            remaining -= take
            if take == buy.quantity:
                lots[tx.asset_id].pop(0)
            else:
                buy.quantity -= take

    out = []
    for asset_id, buys in lots.items():
        if not buys or not buys[-1].quantity:
            continue
        asset = buys[-1].asset
        pp = PricePoint.objects.filter(
            asset_type=asset.asset_type, identifier=asset.identifier
        ).first()
        qty = sum(b.quantity for b in buys)
        cost = sum((b.price * b.quantity for b in buys), Decimal(0))
        current = qty * pp.price if pp else cost
        out.append(UnrealizedLot(
            asset_name=asset.name, asset_type=asset.asset_type,
            buy_date=buys[0].date, quantity=qty, cost=cost,
            current=current.quantize(Decimal("0.01")),
            asset_pk=asset_id, isin=asset.isin,
            user_ids=frozenset(b.account.user_id for b in buys),
        ))
    return sorted(out, key=lambda l: l.gain)


def dividends_for_fy(fy: str) -> Decimal:
    """Dividend/IDCW received in a financial year — taxed at slab."""
    from django.db.models import Sum

    start = date(int(fy[:4]), 4, 1)
    end = date(int(fy[:4]) + 1, 3, 31)
    return (Transaction.objects.filter(
        kind=Transaction.Kind.DIVIDEND, date__gte=start, date__lte=end
    ).aggregate(t=Sum("amount"))["t"] or Decimal(0))


# Cost Inflation Index (CBDT notified values), FY label -> index
CII = {
    "2001-02": 100, "2002-03": 105, "2003-04": 109, "2004-05": 113, "2005-06": 117,
    "2006-07": 122, "2007-08": 129, "2008-09": 137, "2009-10": 148, "2010-11": 167,
    "2011-12": 184, "2012-13": 200, "2013-14": 220, "2014-15": 240, "2015-16": 254,
    "2016-17": 264, "2017-18": 272, "2018-19": 280, "2019-20": 289, "2020-21": 301,
    "2021-22": 317, "2022-23": 331, "2023-24": 348, "2024-25": 363, "2025-26": 376,
    "2026-27": 389,  # estimate — CBDT notifies in July
}

LTCG_RATE_RE = Decimal("0.125")   # 12.5% without indexation
LTCG_RATE_IDX = Decimal("0.20")   # 20% with indexation (pre-23-Jul-2024 buys)
PROPERTY_LONG_TERM_DAYS = 730     # 24 months for immovable property


def real_estate_whatif(holding, sale_price: Decimal) -> dict | None:
    """Tax impact of selling an immovable property at `sale_price`.

    Post-July-2024 rules: long term (>24 months) LTCG is 12.5% without
    indexation; for property bought before 23-Jul-2024, a resident can pick
    20% with indexation instead (whichever is lower). Short term is taxed at
    slab. Exemptions: Sec 54 (reinvest in residential house), Sec 54EC bonds
    (up to ₹50L), Sec 54F.
    """
    if holding.asset.asset_type != Asset.AssetType.REAL_ESTATE:
        return None
    buy_date = (holding.asset.transactions.order_by("date").first() or
                holding.asset.transactions.order_by("-date").first())
    if buy_date is None:
        return None
    cost = holding.invested
    buy_fy = _fy_of(buy_date.date)
    sale_fy = _fy_of(date.today())
    cii_buy, cii_sale = CII.get(buy_fy), CII.get(sale_fy, CII["2026-27"])

    days = (date.today() - buy_date.date).days
    long_term = days > PROPERTY_LONG_TERM_DAYS
    gain = sale_price - cost
    out = {
        "name": holding.asset.name, "buy_date": buy_date.date, "cost": cost,
        "sale_price": sale_price, "gain": gain, "long_term": long_term,
        "old_buy": buy_date.date < date(2024, 7, 23),
    }
    if not long_term:
        out["tax"] = None  # slab rate — reported, not computed
        out["note"] = "Held under 24 months: gains are taxed at your income-tax slab."
        return out

    tax_125 = max(Decimal(0), gain) * LTCG_RATE_RE
    out["tax_125"] = tax_125
    if out["old_buy"] and cii_buy and cii_sale:
        indexed_cost = cost * cii_sale / cii_buy
        idx_gain = sale_price - indexed_cost
        tax_idx = max(Decimal(0), idx_gain) * LTCG_RATE_IDX
        out["indexed_cost"] = indexed_cost.quantize(Decimal("0.01"))
        out["tax_idx"] = tax_idx
        out["tax"] = min(tax_125, tax_idx)
        out["regime"] = ("20% with indexation" if tax_idx < tax_125
                         else "12.5% without indexation")
    else:
        out["tax"] = tax_125
        out["regime"] = "12.5% without indexation"
    out["cess"] = out["tax"] * Decimal("0.04")  # 4% health & education cess
    out["total_tax"] = out["tax"] + out["cess"]
    out["tds"] = sale_price >= Decimal("5000000")
    out["exemptions"] = [
        "Sec 54: buy another residential house (1 yr before – 2 yrs after sale) or build one (3 yrs) — full exemption if cost ≥ gain.",
        "Sec 54EC: invest the gain in NHAI/REC capital-gain bonds within 6 months — up to ₹50L, 5-yr lock-in.",
        "Unspent gains: park in a Capital Gains Account Scheme (bank) before the ITR due date to keep Sec 54/54F alive.",
    ]
    return out


def scheme_flows_for_fy(user, fy: str) -> list[dict]:
    """Balance-type schemes (PPF/EPF/NPS/FD/…) year summary, like the older
    app's EPF/PPF tables: opening invested, deposits, withdrawals, current."""
    from django.db.models import Q, Sum

    year = int(fy[:4])
    start, end = date(year, 4, 1), date(year + 1, 3, 31)
    schemes = (
        Transaction.objects.filter(
            account__user=user,
            asset__asset_type__in=Asset.BALANCE_TYPES,
        )
        .values("asset_id", "asset__name", "asset__asset_type")
        .distinct()
    )
    rows = []
    for s in schemes:
        base = Transaction.objects.filter(asset_id=s["asset_id"])
        opening = base.filter(date__lt=start).aggregate(
            q=Sum("quantity", filter=Q(kind=Transaction.Kind.BUY)),
            w=Sum("quantity", filter=Q(kind=Transaction.Kind.SELL))) or {}
        deposits = base.filter(date__gte=start, date__lte=end,
                               kind=Transaction.Kind.BUY).aggregate(t=Sum("amount"))["t"] or Decimal(0)
        withdrawals = base.filter(date__gte=start, date__lte=end,
                                  kind=Transaction.Kind.SELL).aggregate(t=Sum("amount"))["t"] or Decimal(0)
        opening_amt = (opening["q"] or Decimal(0)) - (opening["w"] or Decimal(0))
        holding = Holding.objects.filter(asset_id=s["asset_id"], account__user=user).first()
        rows.append({
            "label": dict(Asset.AssetType.choices).get(s["asset__asset_type"], s["asset__asset_type"]),
            "name": s["asset__name"],
            "opening": opening_amt,
            "deposits": deposits,
            "withdrawals": withdrawals,
            "current": holding.current_value or holding.invested if holding else Decimal(0),
        })
    return sorted(rows, key=lambda r: -r["current"])
