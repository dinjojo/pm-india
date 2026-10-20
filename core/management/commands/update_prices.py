"""Price feeds: AMFI daily NAV file for mutual funds (free, official).

https://www.amfiindia.com/spages/NAVAll.txt — one line per scheme (8 columns):
    Scheme Code; ISIN Div Payout/Growth; ISIN Div Reinvestment; Scheme Name;
    Plan; Option; NAV; Date
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
    mf_assets = {
        a.identifier: a
        for a in Asset.objects.filter(asset_type=Asset.AssetType.MUTUAL_FUND).exclude(identifier="")
    }
    mf_identifiers = set(mf_assets)
    if not mf_identifiers:
        return 0

    resp = requests.get(AMFI_NAV_URL, timeout=30)
    resp.raise_for_status()
    updated = 0
    for line in io.StringIO(resp.text):
        parts = [p.strip() for p in line.split(";")]
        if len(parts) != 8:
            continue  # header, section titles, blank lines
        isin_payout, isin_reinv, nav_raw, as_of_raw = parts[1], parts[2], parts[6], parts[7]
        match = next(
            (i for i in (isin_payout, isin_reinv) if i and i != "-" and i in mf_identifiers),
            None,
        )
        if not match:
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
        # Backfill the AMFI scheme code so holdings deep-link to the fund page.
        asset = mf_assets[match]
        if not (asset.metadata.get("scheme_code") or asset.metadata.get("amfi_code")):
            try:
                code = int(parts[0])
            except ValueError:
                code = 0
            if code > 0:
                asset.metadata = {**asset.metadata, "scheme_code": code}
                asset.save(update_fields=["metadata"])
        updated += 1
    return updated


class Command(BaseCommand):
    help = "Refresh latest prices (AMFI NAVs today; stocks/crypto feeds pluggable)"

    def handle(self, *args, **options):
        n = update_mf_prices()
        self.stdout.write(self.style.SUCCESS(f"Updated {n} MF prices as of {date.today()}"))
