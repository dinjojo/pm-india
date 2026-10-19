"""Price feeds: AMFI daily NAV file for mutual funds (free, official).

https://www.amfiindia.com/spages/NAVAll.txt — one line per scheme:
    ISIN Div Payout; ISIN Reinvestment; Scheme Name; NAV; Date
"""
import io
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

import requests
from django.core.management.base import BaseCommand

from core.models import Asset, PricePoint

AMFI_NAV_URL = "https://www.amfiindia.com/spages/NAVAll.txt"


def update_mf_prices() -> int:
    """Update PricePoints for every MF asset we hold. Returns count updated."""
    mf_identifiers = set(
        Asset.objects.filter(asset_type=Asset.AssetType.MUTUAL_FUND)
        .exclude(identifier="").values_list("identifier", flat=True)
    )
    if not mf_identifiers:
        return 0

    resp = requests.get(AMFI_NAV_URL, timeout=30)
    resp.raise_for_status()
    updated = 0
    for line in io.StringIO(resp.text):
        parts = [p.strip() for p in line.split(";")]
        if len(parts) < 5 or not parts[0]:
            continue
        isin, scheme_nav, nav_raw, as_of_raw = parts[0], parts[1], parts[3], parts[4]
        key = isin or scheme_nav  # direct-plan lines sometimes only carry ISIN growth
        match = isin if isin in mf_identifiers else (scheme_nav if scheme_nav in mf_identifiers else None)
        if not match or match not in mf_identifiers:
            continue
        try:
            nav = Decimal(nav_raw)
            as_of = datetime.strptime(as_of_raw, "%d-%b-%Y").date()
        except (InvalidOperation, ValueError):
            continue
        PricePoint.objects.update_or_create(
            asset_type=Asset.AssetType.MUTUAL_FUND,
            identifier=match,
            defaults={"price": nav, "as_of": as_of, "source": "amfi"},
        )
        updated += 1
    return updated


class Command(BaseCommand):
    help = "Refresh latest prices (AMFI NAVs today; stocks/crypto feeds pluggable)"

    def handle(self, *args, **options):
        n = update_mf_prices()
        self.stdout.write(self.style.SUCCESS(f"Updated {n} MF prices as of {date.today()}"))
