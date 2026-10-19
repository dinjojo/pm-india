"""CAMS / KFintech / NSDL / CDSL CAS PDF import via casparser.

This is the India-specific superpower worth keeping from the original
Portfolio Manager: parse the consolidated account statement offline and turn
it into Transactions. No credentials, no scraping, data stays local.
"""
from dataclasses import dataclass
from decimal import Decimal

from django.contrib.auth.models import User

from core.models import Account, Asset, Transaction


@dataclass
class CasImportResult:
    transactions_created: int = 0
    folios: int = 0


def _get_or_create_asset(scheme: str, isin: str, advisor: str) -> Asset:
    is_direct = "direct" in scheme.lower() or not (advisor or "").strip()
    asset, _ = Asset.objects.get_or_create(
        asset_type=Asset.AssetType.MUTUAL_FUND,
        identifier=isin or f"cas:{scheme}",
        defaults={
            "name": scheme,
            "isin": isin or "",
            "is_direct": is_direct,
        },
    )
    return asset


def import_cas_pdf(user: User, pdf_path: str, password: str | None) -> CasImportResult:
    """Parse a CAS PDF and upsert all folio transactions for `user`."""
    from casparser import read_cas_pdf

    result = CasImportResult()
    with read_cas_pdf(pdf_path, password) as cas:
        account, _ = Account.objects.get_or_create(
            user=user,
            name=f"CAS — {cas.investor_info['name']}",
            defaults={"owner_label": cas.investor_info.get("pan", "")},
        )
        result.folios = len(cas.folios)
        for folio in cas.folios:
            scheme_name = folio["scheme"]
            asset = _get_or_create_asset(scheme_name, folio.get("isin", ""), folio.get("advisor", ""))
            for tx in folio["transactions"]:
                qty = Decimal(str(tx["quantity"] or 0))
                price = Decimal(str(tx["purchase_price"] or 0))
                kind = {
                    "purchase": Transaction.Kind.BUY,
                    "reinvestment": Transaction.Kind.BUY,
                    "sip": Transaction.Kind.BUY,
                    "redemption": Transaction.Kind.SELL,
                    "switch_in_merger": Transaction.Kind.SWITCH_IN,
                    "switch_out_merger": Transaction.Kind.SWITCH_OUT,
                    "dividend_payout": Transaction.Kind.DIVIDEND,
                    "dividend_reinvestment": Transaction.Kind.BUY,
                    "stt_tax": None,
                    "stamp_duty_tax": None,
                }.get(tx["description"].strip().lower())
                if kind is None:
                    continue
                _, created = Transaction.objects.get_or_create(
                    asset=asset,
                    account=account,
                    kind=kind,
                    date=tx["date"],
                    quantity=qty,
                    price=price,
                    defaults={
                        "amount": Decimal(str(tx["amount"] or 0)),
                        "source": "cas",
                        "external_ref": f"cas:{folio['folio']}:{tx['date']}:{tx['description']}:{tx['amount']}",
                    },
                )
                if created:
                    result.transactions_created += 1
    return result
