"""Seed realistic demo holdings across every asset type we support.

Idempotent: tagged with source='seed' + a stable external_ref per row, so
rerunning never duplicates. Realistic ISINs let the AMFI feed price the MF
and SGB; balance-type schemes get deposits plus a current passbook value.
"""
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand
from django.db import transaction as dbtx

from core.models import Account, Asset, Holding, Transaction

SEED = [
    # (type, name, identifier, isin, [(date, kind, qty, price)], current_value)
    ("mf", "Parag Parikh Flexi Cap Direct-G", "INF879O01019", "INF879O01019",
     [("2022-07-01", "buy", "1200", "55.20"), ("2023-08-15", "buy", "800", "62.40"),
      ("2024-11-05", "buy", "500", "74.10"), ("2025-09-20", "sell", "300", "83.50")], None),
    ("mf", "HDFC Mid-Cap Opportunities Direct-G", "INF179K01650", "INF179K01650",
     [("2021-12-01", "buy", "900", "71.30"), ("2023-03-10", "buy", "600", "98.60"),
      ("2024-08-01", "buy", "400", "152.00")], None),
    ("mf", "ICICI Pru Liquid Fund Direct-G", "INF109K01ZS1", "INF109K01ZS1",
     [("2024-01-15", "buy", "500", "385.40")], None),
    ("stock", "Reliance Industries", "RELIANCE", "INE002A01018",
     [("2023-06-01", "buy", "50", "2480.00")], 130250),
    ("stock", "HDFC Bank", "HDFCBANK", "INE040A01034",
     [("2022-01-20", "buy", "40", "1480.00")], 74800),
    ("crypto", "Bitcoin", "bitcoin", "",
     [("2024-03-10", "buy", "0.05", "5230000.00")], 310000),
    ("ppf", "PPF — SBI", "seed:ppf-sbi", "",
     [("2021-07-10", "buy", "150000", "1"), ("2022-07-08", "buy", "150000", "1"),
      ("2023-07-11", "buy", "150000", "1"), ("2024-07-09", "buy", "150000", "1"),
      ("2025-07-08", "buy", "150000", "1")], 1082000),
    ("epf", "EPF — UAN balance", "seed:epf", "",
     [("2021-04-01", "buy", "85000", "1"), ("2022-04-01", "buy", "92000", "1"),
      ("2023-04-01", "buy", "98000", "1"), ("2024-04-01", "buy", "105000", "1")], 436000),
    ("nps", "NPS — Tier I (Aggressive)", "seed:nps", "",
     [("2022-06-01", "buy", "50000", "1"), ("2023-06-05", "buy", "50000", "1"),
      ("2024-06-03", "buy", "50000", "1"), ("2025-06-02", "buy", "50000", "1")], 262000),
    ("fd", "FD — HDFC Bank 7.1% (2027)", "seed:fd-hdfc", "",
     [("2024-06-01", "buy", "300000", "1")], 318500),
    ("fd", "FD — SBI 6.8% (2028)", "seed:fd-sbi", "",
     [("2025-02-15", "buy", "200000", "1")], 205400),
    ("sgb", "SGB 2032 Series II", "SGBAUG24II", "INAV64070114",
     [("2024-08-12", "buy", "12", "6850.00")], 89500),
    ("gold", "Sovereign Gold (physical)", "seed:gold-phys", "",
     [("2022-10-25", "buy", "500000", "1")], 585000),
    ("bond", "SBI Perpetual Bond 7.5%", "seed:bond-sbi", "",
     [("2023-05-02", "buy", "200000", "1")], 208500),
    ("real_estate", "Plot — Devanahalli", "seed:plot-dev", "",
     [("2019-11-30", "buy", "2200000", "1")], 3450000),
    ("other", "P2P Lending — Liquiloans", "seed:p2p", "",
     [("2024-09-01", "buy", "100000", "1")], 104800),
]


class Command(BaseCommand):
    help = "Seed demo holdings across all asset types (idempotent)."

    def add_arguments(self, parser):
        parser.add_argument("--user", default="demo")

    @dbtx.atomic
    def handle(self, *args, **options):
        user = User.objects.get(username=options["user"])
        account, _ = Account.objects.get_or_create(user=user, name="Personal",
                                                   defaults={"owner_label": "Self"})
        created_tx, skipped = 0, 0
        for atype, name, ident, isin, txs, cv in SEED:
            asset, _ = Asset.objects.get_or_create(
                asset_type=atype, identifier=ident,
                defaults={"name": name, "isin": isin},
            )
            for d, kind, qty, price in txs:
                ref = f"seed:{asset.id}:{d}:{kind}:{qty}"
                _, tx_created = Transaction.objects.get_or_create(
                    asset=asset, account=account, date=d, kind=kind,
                    source="seed", external_ref=ref,
                    defaults={"quantity": Decimal(qty), "price": Decimal(price)},
                )
                created_tx += tx_created
            if cv is not None and not asset.holdings.filter(current_value__isnull=False).exists():
                pass  # set after refresh below
        from core.portfolio import refresh_holdings
        refresh_holdings()

        # Balance-type current values, set on holdings (not duplicated by reruns)
        for atype, name, ident, isin, txs, cv in SEED:
            if cv is None:
                continue
            asset = Asset.objects.get(asset_type=atype, identifier=ident)
            for h in asset.holdings.filter(account=account, current_value__isnull=True):
                h.current_value = Decimal(cv)
                h.save()

        self.stdout.write(self.style.SUCCESS(
            f"Seeded {created_tx} new transactions for {user.username} (skipped {skipped})."))
