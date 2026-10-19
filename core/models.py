"""
Core domain model: one normalized shape for every asset class.

Asset -> Transaction -> Holding -> PricePoint

Asset classes are a typed enum on Asset, so adding e.g. SGBs or NPS is a
migration-free code change, not a new Django app.
"""
from decimal import Decimal

from django.contrib.auth.models import User
from django.db import models


class Asset(models.Model):
    class AssetType(models.TextChoices):
        MUTUAL_FUND = "mf", "Mutual Fund"
        STOCK = "stock", "Stock / ETF"
        CRYPTO = "crypto", "Crypto"
        PPF = "ppf", "PPF"
        EPF = "epf", "EPF"
        NPS = "nps", "NPS"
        FD = "fd", "Fixed Deposit"
        SGB = "sgb", "Sovereign Gold Bond"
        GOLD = "gold", "Gold"
        BOND = "bond", "Bond"
        REAL_ESTATE = "real_estate", "Real Estate"
        OTHER = "other", "Other"
        SAVINGS = "savings", "Bank Account"

    EQUITY_LIKE = {AssetType.MUTUAL_FUND, AssetType.STOCK, AssetType.CRYPTO,
                   AssetType.SGB, AssetType.GOLD}  # LTCG/STCG rules apply
    BALANCE_TYPES = {AssetType.PPF, AssetType.EPF, AssetType.NPS, AssetType.FD,
                     AssetType.BOND, AssetType.REAL_ESTATE, AssetType.OTHER}
    # Liquidity buckets: what can actually be touched, and when
    LIQUIDITY = {AssetType.MUTUAL_FUND: "liquid", AssetType.STOCK: "liquid",
                 AssetType.CRYPTO: "liquid", AssetType.SAVINGS: "liquid",
                 AssetType.SGB: "liquid",  # tradable on exchanges
                 AssetType.PPF: "locked", AssetType.EPF: "locked", AssetType.NPS: "locked",
                 AssetType.FD: "locked",
                 AssetType.GOLD: "illiquid", AssetType.BOND: "illiquid",
                 AssetType.REAL_ESTATE: "illiquid", AssetType.OTHER: "illiquid"}
    LIQUIDITY_LABELS = {"liquid": "Liquid — sell anytime",
                        "locked": "Locked-in — PPF / EPF / NPS / FD tenures",
                        "illiquid": "Illiquid — physical or long-horizon"}

    asset_type = models.CharField(max_length=16, choices=AssetType.choices)
    name = models.CharField(max_length=250)
    # Starred by the user: pinned first on dashboard/holdings, shown in tax views
    priority = models.BooleanField(default=False, db_index=True)
    # ISIN for MFs/bonds/SGBs, NSE symbol for stocks, coin id for crypto
    identifier = models.CharField(max_length=32, db_index=True, blank=True, default="")
    isin = models.CharField(max_length=12, blank=True, default="", db_index=True)
    # regular vs direct plans matter in India; flag it like Kuvera/Groww do
    is_direct = models.BooleanField(default=True)
    metadata = models.JSONField(blank=True, default=dict)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["asset_type", "identifier"], condition=~models.Q(identifier=""),
                name="uniq_asset_identifier_per_type",
            )
        ]

    def __str__(self):
        return f"{self.name} ({self.get_asset_type_display()})"


class Account(models.Model):
    """Where the asset lives: a demat account, AMC folio group, wallet, etc."""

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="accounts")
    name = models.CharField(max_length=120)
    # family/multi-PAN support: tag which member owns this account
    owner_label = models.CharField(max_length=80, blank=True, default="")
    notes = models.TextField(blank=True, default="")

    def __str__(self):
        return self.name


class Transaction(models.Model):
    class Kind(models.TextChoices):
        BUY = "buy", "Buy / SIP"
        SELL = "sell", "Sell / Redemption"
        DIVIDEND = "dividend", "Dividend / IDCW"
        SWITCH_IN = "switch_in", "Switch In"
        SWITCH_OUT = "switch_out", "Switch Out"

    asset = models.ForeignKey(Asset, on_delete=models.CASCADE, related_name="transactions")
    account = models.ForeignKey(Account, on_delete=models.CASCADE, related_name="transactions")
    kind = models.CharField(max_length=12, choices=Kind.choices)
    date = models.DateField(db_index=True)
    quantity = models.DecimalField(max_digits=20, decimal_places=4, default=0)
    price = models.DecimalField(max_digits=20, decimal_places=4, default=0)
    amount = models.DecimalField(max_digits=20, decimal_places=2, default=0)
    source = models.CharField(max_length=32, default="manual", db_index=True)  # manual | cas | zerodha
    external_ref = models.CharField(max_length=120, blank=True, default="")

    class Meta:
        ordering = ["date"]
        indexes = [models.Index(fields=["source", "external_ref"])]

    def save(self, *args, **kwargs):
        if not self.amount and self.quantity and self.price:
            self.amount = (self.quantity * self.price).quantize(Decimal("0.01"))
        super().save(*args, **kwargs)


class Holding(models.Model):
    """Materialized per (asset, account) position, refreshed from transactions."""

    asset = models.ForeignKey(Asset, on_delete=models.CASCADE, related_name="holdings")
    account = models.ForeignKey(Account, on_delete=models.CASCADE, related_name="holdings")
    quantity = models.DecimalField(max_digits=20, decimal_places=4, default=0)
    invested = models.DecimalField(max_digits=20, decimal_places=2, default=0)
    # Manual override for balance-type assets (PPF passbook, FD maturity value,
    # EPF balance) that no price feed covers. None means "fall back to invested".
    current_value = models.DecimalField(max_digits=20, decimal_places=2, null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["asset", "account"], name="uniq_holding")
        ]

    @property
    def avg_cost(self) -> Decimal:
        return (self.invested / self.quantity) if self.quantity else Decimal(0)


class PricePoint(models.Model):
    """Latest known price per asset identifier (AMFI NAV, NSE close, manual)."""

    asset_type = models.CharField(max_length=16)
    identifier = models.CharField(max_length=32, unique=True)
    price = models.DecimalField(max_digits=20, decimal_places=4)
    as_of = models.DateField()
    source = models.CharField(max_length=32, default="manual")
    updated_at = models.DateTimeField(auto_now=True)


class ValueSnapshot(models.Model):
    """One row per user per day: portfolio totals, for the trend chart.

    Snapshots from the daily job are exact; backfilled ones are reconstructed
    from historical NAVs (MFs) and deposits (other classes) — estimates.
    """

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="value_snapshots")
    date = models.DateField(db_index=True)
    invested = models.DecimalField(max_digits=20, decimal_places=2, default=0)
    current = models.DecimalField(max_digits=20, decimal_places=2, default=0)
    breakdown = models.JSONField(default=dict)  # {asset_type: current}
    estimated = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user", "date"], name="uniq_snapshot_user_date")
        ]
        ordering = ["date"]


class GoalPlan(models.Model):
    """A financial goal, ported from the Freefincal robo-advisor workbook.

    kind=one_time  — Step 4A: a cost at a future date (house, car, wedding).
    kind=recurring — Step 4B: a growing annuity of annual costs (school fees).
    kind=retirement — Steps 1-3: inflation-protected income to life expectancy.
    Values are recomputed on every view; nothing here is stale.
    """

    class Kind(models.TextChoices):
        ONE_TIME = "one_time", "One-time goal"
        RECURRING = "recurring", "Recurring goal"
        RETIREMENT = "retirement", "Retirement"

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="goal_plans")
    name = models.CharField(max_length=120)
    kind = models.CharField(max_length=12, choices=Kind.choices)
    flexible = models.BooleanField(default=False,
                                   help_text="Can be delayed a few years → more equity allowed")
    # one-time
    cost_today = models.DecimalField(max_digits=20, decimal_places=2, null=True, blank=True,
                                     help_text="Cost if spent today")
    years_to_goal = models.PositiveIntegerField(null=True, blank=True)
    # recurring
    annual_cost = models.DecimalField(max_digits=20, decimal_places=2, null=True, blank=True)
    years_duration = models.PositiveIntegerField(null=True, blank=True)
    years_until_start = models.PositiveIntegerField(null=True, blank=True)
    # retirement
    current_age = models.PositiveIntegerField(null=True, blank=True)
    retire_age = models.PositiveIntegerField(null=True, blank=True)
    monthly_expense = models.DecimalField(max_digits=20, decimal_places=2, null=True, blank=True,
                                          help_text="Monthly expense that persists in retirement")
    annual_expense = models.DecimalField(max_digits=20, decimal_places=2, null=True, blank=True,
                                         help_text="Annual expense that persists (insurances etc.)")
    # common
    years_to_start = models.PositiveIntegerField(default=0,
                                                 help_text="Years from now you begin investing (0 = now)")
    inflation_override = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True,
                                             help_text="Goal inflation; blank = 10% default")
    current_equity = models.DecimalField(max_digits=20, decimal_places=2, default=0)
    current_taxable_fi = models.DecimalField(max_digits=20, decimal_places=2, default=0)
    current_taxfree_fi = models.DecimalField(max_digits=20, decimal_places=2, default=0)
    lumpsum_expected = models.DecimalField(max_digits=20, decimal_places=2, default=0,
                                           help_text="Maturity/payout expected near the goal date")
    step_up_pct = models.DecimalField(max_digits=5, decimal_places=2, default=5,
                                      help_text="Annual increase in the monthly SIP")
    # retirement: post-retirement income (monthly, today's ₹) + annual escalation %
    rent_income = models.DecimalField(max_digits=20, decimal_places=2, null=True, blank=True)
    rent_escalation = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    pension_income = models.DecimalField(max_digits=20, decimal_places=2, null=True, blank=True)
    pension_escalation = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    other_income = models.DecimalField(max_digits=20, decimal_places=2, null=True, blank=True)
    other_escalation = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    # retirement: window of each stream in retirement years, 0-based; start included,
    # stop excluded. Blank start = from retirement, blank stop = for life.
    rent_start = models.IntegerField(null=True, blank=True)
    rent_stop = models.IntegerField(null=True, blank=True)
    pension_start = models.IntegerField(null=True, blank=True)
    pension_stop = models.IntegerField(null=True, blank=True)
    other_start = models.IntegerField(null=True, blank=True)
    other_stop = models.IntegerField(null=True, blank=True)
    # retirement: inflation-indexed annuity floor, % of first-year expense (0 = off)
    pension_floor_pct = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    # Step 2 A24: take-home pay + employer/employee EPF/NPS, for the SIP affordability check
    monthly_take_home = models.DecimalField(max_digits=20, decimal_places=2, null=True, blank=True)

    def income_streams(self) -> list[tuple[float, float, int, int | None]]:
        """[(monthly ₹ today, escalation %, start year, stop year or None)]."""
        out = []
        for k in ("rent", "pension", "other"):
            stop = getattr(self, f"{k}_stop")
            out.append((float(getattr(self, f"{k}_income") or 0),
                        float(getattr(self, f"{k}_escalation") or 0),
                        int(getattr(self, f"{k}_start") or 0),
                        int(stop) if stop is not None else None))
        return out

    def __str__(self):
        return f"{self.name} ({self.get_kind_display()})"


class RetirementSettings(models.Model):
    """Singleton of workbook Settings-sheet assumptions (row pk=1).
    Defaults are the July 2025 workbook values."""

    inflation_working = models.DecimalField(max_digits=5, decimal_places=2, default=6,
                                            help_text="Inflation before retirement % (Settings!C3)")
    inflation_retired = models.DecimalField(max_digits=5, decimal_places=2, default=6,
                                            help_text="Inflation during retirement % (Settings!C5)")
    equity_return = models.DecimalField(max_digits=5, decimal_places=2, default=10,
                                        help_text="Post-tax equity return % (Settings!C7)")
    taxable_fi_return = models.DecimalField(max_digits=5, decimal_places=2, default=6,
                                            help_text="Post-tax taxable FI return % (FD, debt funds)")
    taxfree_return = models.DecimalField(max_digits=5, decimal_places=2, default=7,
                                         help_text="Tax-free FI return % — PPF/EPF (Settings!C8)")
    life_expectancy = models.PositiveIntegerField(default=90, help_text="Settings!C4")

    class Meta:
        verbose_name_plural = "retirement settings"

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def load(cls) -> "RetirementSettings":
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def to_assumptions(self):
        from core.goals import Assumptions
        return Assumptions(inflation=float(self.inflation_working),
                           inflation_retirement=float(self.inflation_retired),
                           equity_return=float(self.equity_return),
                           fixed_return=float(self.taxable_fi_return),
                           taxfree_return=float(self.taxfree_return),
                           life_expectancy=int(self.life_expectancy))
