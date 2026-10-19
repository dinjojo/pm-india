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
from datetime import date as Date, datetime
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


def _date(x) -> Date | None:
    """casparser dates: date objects (CAMS/KFin rows) or strings — ISO,
    NSDL '18-Oct-2026', CDSL '18/10/2026' / '18-10-2026'. None if unparseable."""
    if isinstance(x, Date):
        return x
    text = str(x or "").strip()
    for candidate, fmt in ((text[:10], "%Y-%m-%d"), (text, "%d-%b-%Y"), (text, "%d/%b/%Y"),
                           (text, "%d/%m/%Y"), (text, "%d-%m-%Y")):
        try:
            return datetime.strptime(candidate, fmt).date()
        except ValueError:
            continue
    return None


def _get_or_create_mf(scheme: str, isin: str, advisor: str | None) -> Asset:
    # advisor=None means "unknown" (demat statements carry no ARN): judge by
    # the plan name alone instead of assuming no advisor = direct.
    if advisor is None:
        is_direct = "direct" in scheme.lower()
    else:
        is_direct = "direct" in scheme.lower() or not advisor.strip()
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
    # Keyed on the per-folio/per-demat-account ref, not on (date, qty, price):
    # the same scheme bought the same day in two folios (or the same ISIN in
    # an NSDL and a CDSL account) are distinct positions. update_or_create
    # also repairs rows written by earlier imports (e.g. signed redemptions).
    _, created = Transaction.objects.update_or_create(
        asset=asset, account=account, source="cas", external_ref=ref,
        defaults={"kind": kind, "date": tx_date, "quantity": qty,
                  "price": price, "amount": amount},
    )
    if created:
        user_result.transactions_created += 1


def _import_cams_kfin(cas, result: CasImportResult, account: Account) -> None:
    for folio in cas.folios:
        for scheme in folio.schemes:
            asset = _get_or_create_mf(scheme.scheme, scheme.isin or "", scheme.advisor or "")
            for tx in scheme.transactions:
                ttype = getattr(tx.type, "value", str(tx.type))
                kind = _KIND_MAP.get(ttype)
                if kind is None:
                    continue  # taxes / reversals / segregation: no position change
                qty = _d(tx.units)
                amount = _d(tx.amount)
                tx_date = _date(tx.date)
                if tx_date is None:
                    result.warnings.append(
                        f"Folio {folio.folio} · {scheme.scheme}: skipped a {ttype} row "
                        f"with unreadable date {tx.date!r}.")
                    continue
                price = _d(tx.nav)
                if not price and qty:
                    price = (abs(amount) / abs(qty)).quantize(Decimal("0.0001"))
                # Ref keeps casparser's signed values so earlier imports match.
                ref = f"cas:{folio.folio}:{scheme.isin or scheme.scheme}:{ttype}:{tx.date}:{qty}:{amount}"
                # casparser signs outflows (redemption / switch-out units and
                # amount are negative); our kinds carry the direction and
                # refresh_holdings / tax.gains subtract SELL quantities, so
                # store magnitudes or a redemption would ADD units.
                _add_tx(result, asset, account, kind, tx_date, abs(qty), abs(price), abs(amount), ref)


def _import_nsdlcdsl(cas, result: CasImportResult, account: Account) -> None:
    """NSDL/CDSL demat statements are holdings snapshots, not transaction
    history: one opening BUY per position, dated at the statement's end."""
    # casparser leaves the period as "" when it cannot find it, and NSDL/CDSL
    # write it as '18-Oct-2026' / '18/10/2026', never ISO.
    period_to = cas.statement_period.to if cas.statement_period else ""
    as_of = _date(period_to) or Date.today()
    if period_to and _date(period_to) is None:
        result.warnings.append(f"Statement date {period_to!r} unreadable; positions dated today.")
    for acc in cas.accounts:
        tag = f"{acc.dp_id or ''}:{acc.client_id or ''}".strip(":") or acc.name
        for mf in acc.mutual_funds:
            if not mf.balance:
                continue
            asset = _get_or_create_mf(mf.name or f"MF {mf.isin}", mf.isin, None)
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
