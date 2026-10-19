"""CAMS / KFintech / NSDL / CDSL CAS PDF import via casparser.

This is the India-specific superpower worth keeping from the original
Portfolio Manager: parse the consolidated account statement offline and turn
it into Transactions. No credentials, no scraping, data stays local.

casparser 1.x API notes (breaking vs the 0.x dict API this module originally
used):
- read_cas_pdf() returns a plain pydantic model, NOT a context manager.
  CASData (CAMS/KFin) has folios -> schemes -> transactions; NSDLCASData
  (NSDL/CDSL) has demat accounts holding mutual_funds / equities / bonds as
  position snapshots (no transaction history).
- TransactionData.type is a TransactionType enum, so mapping to our kinds no
  longer string-matches descriptions.
"""
from dataclasses import dataclass, field
from datetime import date as Date
from decimal import Decimal

from django.contrib.auth.models import User

from core.models import Account, Asset, Transaction


@dataclass
class CasImportResult:
    transactions_created: int = 0
    folios: int = 0
    demat_positions: int = 0
    warnings: list[str] = field(default_factory=list)


_KIND_MAP = {
    "PURCHASE": Transaction.Kind.BUY,
    "PURCHASE_SIP": Transaction.Kind.BUY,
    "REDEMPTION": Transaction.Kind.SELL,
    "DIVIDEND_PAYOUT": Transaction.Kind.DIVIDEND,
    "DIVIDEND_REINVEST": Transaction.Kind.BUY,
    "SWITCH_IN": Transaction.Kind.SWITCH_IN,
    "SWITCH_IN_MERGER": Transaction.Kind.SWITCH_IN,
    "SWITCH_OUT": Transaction.Kind.SWITCH_OUT,
    "SWITCH_OUT_MERGER": Transaction.Kind.SWITCH_OUT,
    # SEGREGATION / REVERSAL / taxes / MISC do not map to position changes we
    # can model; skipped with the count preserved in warnings.
}
_SKIPPED_KINDS = {"SEGREGATION", "REVERSAL", "STT_TAX", "STAMP_DUTY_TAX", "TDS_TAX", "MISC", "GIFT_IN", "GIFT_OUT"}


def _d(x) -> Decimal:
    return Decimal(str(x)) if x is not None else Decimal("0")


def _date(x) -> Date:
    return x if isinstance(x, Date) else Date.fromisoformat(str(x)[:10])


def _get_or_create_mf(scheme: str, isin: str, advisor: str | None) -> Asset:
    is_direct = "direct" in scheme.lower() or not (advisor or "").strip()
    asset, _ = Asset.objects.get_or_create(
        asset_type=Asset.AssetType.MUTUAL_FUND,
        identifier=isin or f"cas:{scheme}",
        defaults={"name": scheme, "isin": isin or "", "is_direct": is_direct},
    )
    return asset


def _get_or_create_stock(name: str, isin: str) -> Asset:
    asset, _ = Asset.objects.get_or_create(
        asset_type=Asset.AssetType.STOCK,
        identifier=isin or f"cas:{name}",
        defaults={"name": name, "isin": isin or ""},
    )
    return asset


def _get_or_create_bond(name: str, isin: str) -> Asset:
    asset, _ = Asset.objects.get_or_create(
        asset_type=Asset.AssetType.BOND,
        identifier=isin or f"cas:{name}",
        defaults={"name": name, "isin": isin or ""},
    )
    return asset


def _add_tx(user_result: CasImportResult, asset: Asset, account: Account, kind, tx_date: Date,
            qty: Decimal, price: Decimal, amount: Decimal, ref: str) -> None:
    _, created = Transaction.objects.get_or_create(
        asset=asset, account=account, kind=kind, date=tx_date,
        quantity=qty, price=price,
        defaults={"amount": amount, "source": "cas", "external_ref": ref},
    )
    if created:
        user_result.transactions_created += 1


def _import_cams_kfin(cas, result: CasImportResult, account: Account) -> None:
    for folio in cas.folios:
        for scheme in folio.schemes:
            asset = _get_or_create_mf(scheme.scheme, scheme.isin or "", scheme.advisor or "")
            for tx in scheme.transactions:
                kind = _KIND_MAP.get(getattr(tx.type, "value", str(tx.type)))
                if kind is None:
                    if getattr(tx.type, "value", str(tx.type)) in _SKIPPED_KINDS:
                        continue
                    continue
                qty = _d(tx.units)
                amount = _d(tx.amount)
                price = _d(tx.nav)
                if not price and qty:
                    price = (abs(amount) / abs(qty)).quantize(Decimal("0.0001"))
                ref = f"cas:{folio.folio}:{scheme.isin or scheme.scheme}:{tx.type.value}:{tx.date}:{qty}:{amount}"
                _add_tx(result, asset, account, kind, _date(tx.date), qty, price, amount, ref)


def _import_nsdlcdsl(cas, result: CasImportResult, account: Account) -> None:
    """NSDL/CDSL demat statements are holdings snapshots, not transaction
    history: one opening BUY per position, dated at the statement's end."""
    as_of = _date(cas.statement_period.to) if cas.statement_period else Date.today()
    for acc in cas.accounts:
        tag = f"{acc.dp_id or ''}:{acc.client_id or ''}".strip(":") or acc.name
        for mf in acc.mutual_funds:
            if not mf.balance:
                continue
            asset = _get_or_create_mf(mf.name or f"MF {mf.isin}", mf.isin, "")
            price = _d(mf.avg_cost) if mf.avg_cost else _d(mf.nav)
            _add_tx(result, asset, account, Transaction.Kind.BUY, as_of,
                    _d(mf.balance), price, _d(mf.total_cost) if mf.total_cost else _d(mf.value),
                    f"cas-demat:{tag}:{mf.isin}")
            result.demat_positions += 1
        for eq in acc.equities:
            if not eq.num_shares:
                continue
            asset = _get_or_create_stock(eq.name or f"Equity {eq.isin}", eq.isin)
            _add_tx(result, asset, account, Transaction.Kind.BUY, as_of,
                    _d(eq.num_shares), _d(eq.price), _d(eq.value),
                    f"cas-demat:{tag}:{eq.isin}")
            result.demat_positions += 1
        for bond in acc.bonds:
            if not bond.num_bonds:
                continue
            asset = _get_or_create_bond(bond.name or f"Bond {bond.isin}", bond.isin)
            price = _d(bond.market_price) if bond.market_price else _d(bond.face_value)
            _add_tx(result, asset, account, Transaction.Kind.BUY, as_of,
                    _d(bond.num_bonds), price, _d(bond.value),
                    f"cas-demat:{tag}:{bond.isin}")
            result.demat_positions += 1


def import_cas_pdf(user: User, pdf_path: str, password: str | None) -> CasImportResult:
    """Parse a CAS PDF and upsert all folio transactions for `user`."""
    from casparser import read_cas_pdf

    result = CasImportResult()
    cas = read_cas_pdf(pdf_path, password or "")
    investor = cas.investor_info
    account, _ = Account.objects.get_or_create(
        user=user,
        name=f"CAS — {investor.name}",
        defaults={"owner_label": ""},
    )
    if cas.parse_warnings:
        result.warnings = [str(w) for w in cas.parse_warnings][:5]

    # CAMS / KFin: folio transactions. NSDL / CDSL: demat position snapshots.
    folios = getattr(cas, "folios", None)
    if folios is not None:
        result.folios = len(folios)
        _import_cams_kfin(cas, result, account)
    else:
        accounts = getattr(cas, "accounts", [])
        result.folios = len(accounts)
        _import_nsdlcdsl(cas, result, account)
    return result
