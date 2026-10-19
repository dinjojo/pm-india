"""Portfolio rollup: refresh Holdings from Transactions, compute dashboard views."""
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from django.db.models import Q, Sum

from core.models import Asset, Holding, PricePoint, Transaction


def refresh_holdings() -> int:
    """Recompute every Holding from its transactions (average-cost basis).

    Holdings are upserted from the transaction groups, so new asset/account
    combinations get created here, and orphans (all transactions deleted)
    get removed.
    """
    changed = 0
    groups = list(Transaction.objects.values_list("asset_id", "account_id").distinct())
    for asset_id, account_id in groups:
        txs = Transaction.objects.filter(asset_id=asset_id, account_id=account_id)
        agg = txs.aggregate(
            bought_qty=Sum("quantity", filter=Q(kind__in=[Transaction.Kind.BUY, Transaction.Kind.SWITCH_IN])),
            sold_qty=Sum("quantity", filter=Q(kind__in=[Transaction.Kind.SELL, Transaction.Kind.SWITCH_OUT])),
            bought_amt=Sum("amount", filter=Q(kind__in=[Transaction.Kind.BUY, Transaction.Kind.SWITCH_IN])),
            sold_amt=Sum("amount", filter=Q(kind__in=[Transaction.Kind.SELL, Transaction.Kind.SWITCH_OUT])),
        )
        qty = (agg["bought_qty"] or 0) - (agg["sold_qty"] or 0)
        invested = (agg["bought_amt"] or 0) - (agg["sold_amt"] or 0)
        Holding.objects.update_or_create(
            asset_id=asset_id, account_id=account_id,
            defaults={"quantity": qty, "invested": invested},
        )
        changed += 1
    if groups:
        keep_assets = {asset_id for asset_id, _ in groups}
        Holding.objects.exclude(asset_id__in=keep_assets).delete()
    else:
        Holding.objects.all().delete()
    return changed


@dataclass
class HoldingView:
    holding_pk: int
    asset_pk: int
    name: str
    asset_type: str
    account: str
    owner: str
    quantity: Decimal
    invested: Decimal
    current: Decimal
    # True when current value comes from a manual entry (no price feed for
    # this asset class) — surfaced in the UI so estimates are never passed
    # off as feed-priced data.
    manual_value: bool = False
    priority: bool = False
    asset: Asset | None = None

    @property
    def asset_type_label(self) -> str:
        return dict(Asset.AssetType.choices).get(self.asset_type, self.asset_type.title())

    @property
    def gain(self) -> Decimal:
        return self.current - self.invested

    @property
    def gain_pct(self) -> float:
        return float(self.gain / self.invested * 100) if self.invested else 0.0


@dataclass
class PortfolioView:
    holdings: list = field(default_factory=list)
    total_invested: Decimal = Decimal(0)
    total_current: Decimal = Decimal(0)

    @property
    def total_gain(self) -> Decimal:
        return self.total_current - self.total_invested

    @property
    def total_gain_pct(self) -> float:
        return float(self.total_gain / self.total_invested * 100) if self.total_invested else 0.0

    @property
    def xirr(self) -> float | None:
        flows = []
        for tx in Transaction.objects.filter(
            kind__in=[Transaction.Kind.BUY, Transaction.Kind.SELL]
        ).order_by("date"):
            sign = -1 if tx.kind == Transaction.Kind.BUY else 1
            flows.append((tx.date, float(tx.amount) * sign))
        if not flows:
            return None
        today = date.today()
        value_now = float(self.total_current)
        if value_now:
            flows.append((today, value_now))
        if len(flows) < 2:
            return None
        import pyxirr
        return pyxirr.xirr(flows)

    @property
    def allocation(self) -> dict[str, float]:
        labels = dict(Asset.AssetType.choices)
        out: dict[str, float] = {}
        for h in self.holdings:
            label = labels.get(h.asset_type, h.asset_type)
            out[label] = out.get(label, 0.0) + float(h.current)
        total = sum(out.values()) or 1.0
        return {k: round(v / total * 100, 1) for k, v in sorted(out.items(), key=lambda kv: -kv[1])}


    @property
    def class_breakdown(self) -> list[dict]:
        """Per-asset-class invested/current/weight, for the diversification table."""
        labels = dict(Asset.AssetType.choices)
        agg: dict[str, dict] = {}
        for h in self.holdings:
            row = agg.setdefault(h.asset_type, {
                "type": h.asset_type,
                "label": labels.get(h.asset_type, h.asset_type.title()),
                "invested": Decimal(0), "current": Decimal(0),
                "manual": True,
            })
            row["invested"] += h.invested
            row["current"] += h.current
            if not h.manual_value:
                row["manual"] = False
        rows = sorted(agg.values(), key=lambda r: -r["current"])
        for r in rows:
            r["weight"] = float(r["current"] / self.total_current * 100) if self.total_current else 0.0
            r["gain_pct"] = float((r["current"] - r["invested"]) / r["invested"] * 100) if r["invested"] else 0.0
        return rows

    @property
    def grouped_holdings(self) -> list[dict]:
        """Holdings grouped by asset class, priority assets first in each group."""
        out = []
        for row in self.class_breakdown:
            holdings = [h for h in self.holdings if h.asset_type == row["type"]]
            holdings.sort(key=lambda h: (not h.priority, -h.current))
            out.append({
                "type": row["type"], "label": row["label"],
                "manual": row["manual"],
                "invested": row["invested"], "current": row["current"],
                "gain_pct": row["gain_pct"], "weight": row["weight"],
                "holdings": holdings,
            })
        return out

    def grouped_by(self, key: str) -> list[dict]:
        """Alternate groupings for the Holdings page: account / owner / liquidity.

        Each group: {key, label, holdings, invested, current, gain_pct, weight}.
        """
        if key == "account":
            groups = {}
            for h in self.holdings:
                groups.setdefault(h.account, []).append(h)
            items = list(groups.items())
        elif key == "owner":
            groups = {}
            for h in self.holdings:
                groups.setdefault(h.owner or "Unassigned", []).append(h)
            items = list(groups.items())
        elif key == "liquidity":
            groups = {}
            for h in self.holdings:
                label = Asset.LIQUIDITY.get(h.asset_type, "illiquid")
                groups.setdefault(label, []).append(h)
            order = ["liquid", "locked", "illiquid"]
            items = [(k, groups[k]) for k in order if k in groups]
        else:
            return self.grouped_holdings

        out = []
        for label, holdings in items:
            holdings = sorted(holdings, key=lambda h: (not h.priority, -h.current))
            invested = sum((h.invested for h in holdings), Decimal(0))
            current = sum((h.current for h in holdings), Decimal(0))
            label_map = dict(Asset.LIQUIDITY_LABELS) if key == "liquidity" else {}
            out.append({
                "key": label, "label": label_map.get(label, label),
                "manual": any(h.manual_value for h in holdings),
                "invested": invested, "current": current,
                "gain_pct": float((current - invested) / invested * 100) if invested else 0.0,
                "weight": float(current / self.total_current * 100) if self.total_current else 0.0,
                "holdings": holdings,
            })
        return out

    @property
    def narrative(self) -> str:
        if not self.holdings:
            return "No holdings yet — add an investment or import a CAS statement to get started."
        classes = self.class_breakdown
        top = classes[0]
        manual_n = sum(1 for h in self.holdings if h.manual_value)
        sentence = (f"₹{self.total_current:,.0f} across {len(self.holdings)} holdings in "
                    f"{len(classes)} asset classes; {top['label']} leads at {top['weight']:.0f}% of value.")
        if manual_n:
            sentence += f" {manual_n} holding{'s' if manual_n > 1 else ''} use{'s' if manual_n == 1 else ''} a manually entered value."
        return sentence


def portfolio_view(user) -> PortfolioView:
    view = PortfolioView()
    for h in Holding.objects.select_related("asset", "account").filter(account__user=user):
        pp = PricePoint.objects.filter(
            asset_type=h.asset.asset_type, identifier=h.asset.identifier
        ).first()
        if pp:
            current, manual = h.quantity * pp.price, False
        elif h.current_value is not None:
            current, manual = h.current_value, True
        else:
            current, manual = h.invested, True  # never had a value entered yet
        view.holdings.append(HoldingView(
            holding_pk=h.id,
            asset_pk=h.asset_id,
            name=h.asset.name,
            asset_type=h.asset.asset_type,
            account=h.account.name,
            owner=h.account.owner_label,
            quantity=h.quantity,
            invested=h.invested,
            current=current.quantize(Decimal("0.01")),
            manual_value=manual,
            priority=h.asset.priority,
            asset=h.asset,
        ))
    view.holdings.sort(key=lambda h: (not h.priority, -h.current))
    view.total_invested = sum((h.invested for h in view.holdings), Decimal(0))
    view.total_current = sum((h.current for h in view.holdings), Decimal(0))
    return view
