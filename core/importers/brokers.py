"""Multi-provider holdings import from broker/distributor CSV exports.

Every Indian broker ships a different holdings CSV; mature trackers solve
this with per-provider column mapping. Each provider config lists candidate
header names (case/space-insensitive) for the fields we need. Unmatched
providers fall back to "generic".

Rows become position snapshots: one Buy transaction dated today (or the
export's date column if present), tagged with the provider in external_ref
so re-importing the same file is idempotent.
"""
import csv
import hashlib
import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from django.contrib.auth.models import User

from core.models import Account, Asset, Transaction


@dataclass
class Provider:
    key: str
    name: str
    help: str
    columns: dict[str, list[str]]  # field -> candidate header names
    default_type: str = Asset.AssetType.STOCK

    def match(self, headers: set[str]) -> int:
        """How many required columns this provider can resolve (0 = unusable)."""
        lower = {_norm(h) for h in headers}
        hits = 0
        for names in self.columns.values():
            if any(_norm(n) in lower for n in names):
                hits += 1
        return hits


PROVIDERS = [
    Provider(
        key="zerodha", name="Zerodha Console",
        help="Console → Reports → Portfolio → Holdings → export CSV",
        columns={
            "name": ["symbol", "instrument"],
            "isin": ["isin"],
            "quantity": ["quantity", "qty", "quantity available"],
            "avg_price": ["average price", "buy avg", "average cost", "avg cost"],
        },
    ),
    Provider(
        key="groww", name="Groww",
        help="Portfolio → export holdings CSV",
        columns={
            "name": ["stock name", "name", "scheme name", "instrument"],
            "isin": ["isin"],
            "quantity": ["quantity", "qty", "units"],
            "avg_price": ["average cost", "buy average", "avg price", "average price"],
        },
    ),
    Provider(
        key="hdfc", name="HDFC (Sky / Securities / MF)",
        help="Portfolio → Download holdings CSV",
        columns={
            "name": ["instrument name", "scrip name", "symbol", "scheme name", "name"],
            "isin": ["isin"],
            "quantity": ["quantity", "qty", "units", "balance units"],
            "avg_price": ["average price", "buy price", "avg cost", "purchase price"],
        },
    ),
    Provider(
        key="kotak", name="Kotak (Neo / Securities / MF)",
        help="Portfolio → Download holdings CSV",
        columns={
            "name": ["scrip", "symbol", "instrument", "scheme name", "name"],
            "isin": ["isin"],
            "quantity": ["quantity", "qty", "units", "holding qty"],
            "avg_price": ["average price", "avg price", "buy avg", "avg cost"],
        },
    ),
    Provider(
        key="generic", name="Any CSV (map by column name)",
        help="Symbol/Scheme, ISIN, Qty/Units, Average/Buy price",
        columns={
            "name": ["symbol", "scrip", "instrument", "stock name", "scheme name",
                      "scheme", "name", "company"],
            "isin": ["isin"],
            "quantity": ["quantity", "qty", "units", "balance", "holding"],
            "avg_price": ["average price", "avg price", "buy price", "buy avg",
                           "average cost", "avg cost", "purchase price", "nav"],
        },
    ),
]


@dataclass
class CsvImportResult:
    provider: str = ""
    holdings_created: int = 0
    transactions_created: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)
    asset_names: list[str] = field(default_factory=list)


def _norm(h: str) -> str:
    # Kite writes "Qty." / "Avg. cost"; ICICI-style "(Rs)" suffixes: ignore
    # spaces, underscores, dots and brackets when matching header names.
    return re.sub(r"[\s_.()]+", "", (h or "").strip().lower())


def _pick(row: dict, candidates: list[str]) -> str | None:
    for key, value in row.items():
        if key and _norm(key) in {_norm(c) for c in candidates}:
            text = (value or "").strip().replace(",", "").replace("₹", "")
            if text:
                return text
    return None


def _parse_date(text: str) -> date | None:
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d/%b/%Y", "%d-%b-%Y", "%d %b %Y"):
        try:
            return datetime.strptime(text.strip(), fmt).date()
        except ValueError:
            continue
    return None


def import_holdings_csv(user: User, csv_bytes: bytes, provider_key: str) -> CsvImportResult:
    result = CsvImportResult(provider=provider_key)
    provider = next((p for p in PROVIDERS if p.key == provider_key), None)
    if provider is None:
        result.errors.append(f"Unknown provider: {provider_key}")
        return result

    # Tolerate BOMs and Excel garbage
    text = csv_bytes.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        result.errors.append("The file has no header row.")
        return result

    auto = max(PROVIDERS, key=lambda p: p.match(set(reader.fieldnames)))
    if provider.key == "generic" and auto.match(set(reader.fieldnames)) >= 3:
        provider = auto
        result.provider = auto.key

    resolved: dict[str, list[str]] = provider.columns
    headers = {_norm(h) for h in reader.fieldnames}
    missing = [
        f for f in ("name", "quantity")
        if not any(_norm(c) in headers for c in resolved[f])
    ]
    if missing:
        result.errors.append(
            f"Could not find column(s) for {', '.join(missing)} in this export. "
            f"Columns seen: {', '.join(reader.fieldnames[:12])}"
        )
        return result

    account, _ = Account.objects.get_or_create(user=user, name=f"{provider.name} import")

    for i, row in enumerate(reader, start=2):
        name = _pick(row, resolved["name"])
        qty_raw = _pick(row, resolved["quantity"])
        if not name or not qty_raw:
            result.skipped += 1
            continue
        try:
            qty = Decimal(qty_raw)
            avg = Decimal(_pick(row, resolved["avg_price"]) or "0")
            isin = _pick(row, resolved["isin"]) or ""
            if not (qty.is_finite() and avg.is_finite()):  # 'NaN'/'inf' parse but can't compare
                raise InvalidOperation
        except (InvalidOperation, ArithmeticError):
            result.errors.append(f"Row {i}: could not parse quantity/price for {name!r}.")
            continue
        if qty <= 0:
            result.skipped += 1
            continue

        day_raw = _pick(row, ["date", "as of date", "holding date"])
        day = _parse_date(day_raw) if day_raw else None
        if day_raw and day is None:
            result.errors.append(f"Row {i}: could not read date {day_raw!r} for {name!r}.")
            continue

        # Indian MF units carry INF… ISINs (INE… = equity): a Groww/HDFC/Kotak
        # MF export must land on the same MUTUAL_FUND asset a CAS import uses.
        asset_type = (Asset.AssetType.MUTUAL_FUND if isin.upper().startswith("INF")
                      else provider.default_type)
        asset, created = Asset.objects.get_or_create(
            asset_type=asset_type,
            identifier=isin or f"{provider.key}:{name.lower()[:40]}",
            defaults={"name": name, "isin": isin},
        )
        # Idempotent re-import: a dated row keys on its date; an undated
        # snapshot (dated today) must not re-add the same position when the
        # file is imported again on a later day.
        if day is not None:
            ref = hashlib.sha1(f"{asset.id}:{day}:{qty}:{avg}".encode()).hexdigest()[:16]
        else:
            day = date.today()
            ref = hashlib.sha1(f"{asset.id}:undated:{qty}:{avg}".encode()).hexdigest()[:16]
        tx, tx_created = Transaction.objects.get_or_create(
            asset=asset, account=account, kind=Transaction.Kind.BUY,
            source="csv", external_ref=ref,
            defaults={"quantity": qty, "price": avg, "date": day},
        )
        if created:
            result.holdings_created += 1
        if tx_created:
            result.transactions_created += 1
            result.asset_names.append(name)

    if not result.transactions_created and not result.holdings_created and not result.errors:
        result.errors.append("Nothing new here — these holdings were already imported.")
    return result
