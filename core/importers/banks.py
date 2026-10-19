"""Bank statement import: HDFC, ICICI, Yes Bank, or any CSV with
date/withdrawal/deposit/balance columns.

A statement becomes a Bank Account asset (type 'savings'): every credit is a
deposit, every debit a withdrawal, and the last row's closing balance sets the
account's current value. external_ref tagging makes re-imports idempotent.
Only CSV/TSV text exports are parsed — PDF statements need a different parser.
"""
import csv
import hashlib
import io
import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation

from django.contrib.auth.models import User

from core.models import Account, Asset, Holding, Transaction


@dataclass
class BankProvider:
    key: str
    name: str
    # any of these header-name sets identify the provider (normalised)
    markers: list[list[str]]
    columns: dict[str, list[str]]  # date/withdrawal/deposit/balance/details


BANK_PROVIDERS = [
    BankProvider(
        key="hdfc", name="HDFC Bank",
        markers=[["withdrawalamt", "withdrawal"]],
        columns={
            "date": ["date", "value date"],
            "withdrawal": ["withdrawal amt", "withdrawal amt.", "withdrawal amount", "debit"],
            "deposit": ["deposit amt", "deposit amt.", "deposit amount", "credit"],
            "balance": ["closing balance", "balance"],
            "details": ["narration", "description", "remarks", "particulars"],
        },
    ),
    BankProvider(
        key="icici", name="ICICI Bank",
        markers=[["transactionremarks"], ["depositamount(cr)"]],
        columns={
            "date": ["transaction date", "value date", "date"],
            "withdrawal": ["withdrawal amount (dr)", "withdrawal amount (inr)", "withdrawal amount", "debit"],
            "deposit": ["deposit amount (cr)", "deposit amount (inr)", "deposit amount", "credit"],
            "balance": ["balance", "balance (inr)", "closing balance"],
            "details": ["transaction remarks", "narration", "description"],
        },
    ),
    BankProvider(
        key="yesbank", name="Yes Bank",
        markers=[["particulars"], ["yesbank"]],
        columns={
            "date": ["date", "txn date", "value date"],
            "withdrawal": ["debit", "withdrawal", "dr amount"],
            "deposit": ["credit", "deposit", "cr amount"],
            "balance": ["balance", "closing balance"],
            "details": ["particulars", "narration", "description", "remarks"],
        },
    ),
    BankProvider(
        key="generic", name="Any bank (auto-detect)",
        markers=[],
        columns={
            "date": ["date", "transaction date", "value date", "txn date"],
            "withdrawal": ["withdrawal", "withdrawal amount", "debit", "dr"],
            "deposit": ["deposit", "deposit amount", "credit", "cr"],
            "balance": ["balance", "closing balance"],
            "details": ["narration", "description", "particulars", "remarks", "details"],
        },
    ),
]


@dataclass
class BankImportResult:
    provider: str = ""
    account_name: str = ""
    transactions_created: int = 0
    closing_balance: Decimal | None = None
    skipped: int = 0
    errors: list[str] = field(default_factory=list)


def _norm(h: str) -> str:
    return re.sub(r"[\s_.()]+", "", (h or "").strip().lower())


def _pick(row: dict, candidates: list[str]) -> str | None:
    wanted = {_norm(c) for c in candidates}
    for key, value in row.items():
        if key and _norm(key) in wanted:
            text = (value or "").strip()
            if text:
                return text
    return None


def _money(text: str | None) -> Decimal | None:
    """Parse Indian money cells: '1,23,456.00', '(500.00)', '1000 Cr', '₹50'.
    '(x)', '-x' and an 'x Dr' suffix are negative (an overdrawn balance)."""
    if not text:
        return None
    raw = text.replace("₹", "").replace("Rs.", "").replace("INR", "").strip()
    negative = raw.startswith("(") and raw.endswith(")")
    raw = raw.strip("()").strip()
    suffix = re.search(r"(?i)(cr|dr)\.?$", raw)
    if suffix:
        negative = negative or suffix.group(1).lower() == "dr"
        raw = raw[:suffix.start()]
    cr_dr = raw.strip()
    cr_dr = cr_dr.replace(",", "").replace(" ", "")
    if not cr_dr or not re.match(r"^-?\d*\.?\d+$", cr_dr):
        return None
    value = Decimal(cr_dr)
    if negative:
        value = -value
    return value


def _date(text: str) -> date | None:
    text = (text or "").strip()
    for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%d/%m/%y", "%d-%m-%y",
                "%d %b %Y", "%d-%b-%Y", "%d %b %y", "%Y-%m-%d"):
        try:
            from datetime import datetime
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def import_bank_statement(user: User, csv_bytes: bytes, provider_key: str,
                          account_name: str) -> BankImportResult:
    result = BankImportResult(provider=provider_key, account_name=account_name)
    provider = next((p for p in BANK_PROVIDERS if p.key == provider_key), None)
    if provider is None:
        result.errors.append(f"Unknown bank: {provider_key}")
        return result

    text = csv_bytes.decode("utf-8-sig", errors="replace")
    # Yes Bank sometimes ships tab-separated exports; count delimiters to decide
    first_line = text.splitlines()[0] if text else ""
    delim = "\t" if first_line.count("\t") > first_line.count(",") else ","
    reader = csv.DictReader(io.StringIO(text), delimiter=delim)
    if not reader.fieldnames:
        result.errors.append("The file has no header row.")
        return result

    if provider.key == "generic":
        matched = max(
            (p for p in BANK_PROVIDERS if p.key != "generic"),
            key=lambda p: sum(
                1 for names in p.columns.values()
                if any(_norm(n) in {_norm(h) for h in reader.fieldnames} for n in names)
            ),
            default=None,
        )
        if matched and all(
            any(_norm(n) in {_norm(h) for h in reader.fieldnames} for n in matched.columns[f])
            for f in ("date", "withdrawal", "deposit")
        ):
            provider = matched
            result.provider = matched.key

    headers = {_norm(h) for h in reader.fieldnames}
    for required in ("date", "withdrawal", "deposit"):
        if not any(_norm(c) in headers for c in provider.columns[required]):
            result.errors.append(
                f"Could not find a {required} column in this export. "
                f"Columns seen: {', '.join(reader.fieldnames[:12])}"
            )
            return result

    # No bank named → derive from filename, e.g. "hdfc-statement.csv"
    asset, _ = Asset.objects.get_or_create(
        asset_type=Asset.AssetType.SAVINGS,
        identifier=f"bank:{provider.key}:{account_name.lower()[:40]}",
        defaults={"name": account_name},
    )
    account, _ = Account.objects.get_or_create(user=user, name=account_name)

    balances: list[tuple[date, Decimal]] = []  # (row date, balance) in file order
    found = 0
    rows = list(reader)
    for i, row in enumerate(rows, start=2):
        day_raw = _pick(row, provider.columns["date"])
        day = _date(day_raw or "")
        if day is None:
            result.skipped += 1
            continue
        credit_raw = _pick(row, provider.columns["deposit"])
        debit_raw = _pick(row, provider.columns["withdrawal"])
        credit, debit = _money(credit_raw), _money(debit_raw)
        bad = [t for t, v in ((credit_raw, credit), (debit_raw, debit))
               if v is None and t and t.strip(" -–—").lower() not in {"", "na", "n/a", "nil"}]
        if bad:
            result.errors.append(f"Row {i}: could not read amount {bad[0]!r}; row skipped.")
            result.skipped += 1
            continue
        bal = _money(_pick(row, provider.columns["balance"]))
        if bal is not None:
            balances.append((day, bal))
        if not credit and not debit:
            result.skipped += 1
            continue
        found += 1
        details = _pick(row, provider.columns["details"]) or "Bank transaction"
        # The column says the direction; some exports also sign or bracket
        # debits ("-500", "(500)"). Store magnitudes — refresh_holdings
        # subtracts SELL quantities, so a signed debit would ADD to the balance.
        amount = abs(credit or debit)
        kind = Transaction.Kind.BUY if credit else Transaction.Kind.SELL

        ref = hashlib.sha1(
            f"{asset.id}:{day.isoformat()}:{kind}:{amount}:{i}:{details[:40]}".encode()
        ).hexdigest()[:16]
        tx, created = Transaction.objects.get_or_create(
            asset=asset, account=account, kind=kind, date=day,
            source="bank", external_ref=ref,
            defaults={"quantity": amount, "price": Decimal(1)},
        )
        if created:
            result.transactions_created += 1

    # Closing balance = the chronologically last row. Many banks (ICICI net
    # banking, most apps) export newest-first, so the file's last row would be
    # the OPENING balance there.
    last_balance: Decimal | None = None
    if balances:
        newest_first = balances[0][0] > balances[-1][0]
        last_balance = balances[0][1] if newest_first else balances[-1][1]
        result.closing_balance = last_balance

    if not result.transactions_created:
        if not result.errors:
            result.errors.append(
                "Nothing new here — this statement was already imported." if found
                else "No transactions found in this file.")
        return result

    # Current value = the statement's final closing balance — on THIS user's
    # account only (the savings Asset row is shared by identifier).
    from core.portfolio import refresh_holdings
    refresh_holdings()
    if last_balance is not None:
        for h in Holding.objects.filter(asset=asset, account=account):
            h.current_value = last_balance
            h.save()
    return result
