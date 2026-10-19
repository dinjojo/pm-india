"""Read a photographed statement/slip and draft structured rows from it.

The user says what the document is (text hint), tesseract reads it locally,
and keyword rules draft rows per document type. OCR is never trusted: every
row comes back to the user in an editable table and is imported only after
they confirm.
"""
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

import pytesseract
from PIL import Image

from core.models import Asset

# hint keyword -> asset type (first match wins)
HINT_TYPES: list[tuple[str, str]] = [
    ("bank", Asset.AssetType.SAVINGS), ("savings", Asset.AssetType.SAVINGS),
    ("fd", Asset.AssetType.FD), ("fixed deposit", Asset.AssetType.FD),
    ("ppf", Asset.AssetType.PPF), ("epf", Asset.AssetType.EPF), ("pf", Asset.AssetType.EPF),
    ("nps", Asset.AssetType.NPS),
    ("gold", Asset.AssetType.GOLD), ("sgb", Asset.AssetType.SGB),
    ("bond", Asset.AssetType.BOND), ("debenture", Asset.AssetType.BOND),
    ("property", Asset.AssetType.REAL_ESTATE), ("plot", Asset.AssetType.REAL_ESTATE),
    ("estate", Asset.AssetType.REAL_ESTATE), ("flat", Asset.AssetType.REAL_ESTATE),
    ("crypto", Asset.AssetType.CRYPTO), ("bitcoin", Asset.AssetType.CRYPTO),
    ("mutual fund", Asset.AssetType.MUTUAL_FUND), ("mf", Asset.AssetType.MUTUAL_FUND),
    ("sip", Asset.AssetType.MUTUAL_FUND), ("cas", Asset.AssetType.MUTUAL_FUND),
    ("folio", Asset.AssetType.MUTUAL_FUND),
    ("share", Asset.AssetType.STOCK), ("stock", Asset.AssetType.STOCK),
    ("demat", Asset.AssetType.STOCK), ("broker", Asset.AssetType.STOCK),
    ("equity", Asset.AssetType.STOCK),
]
DEFAULT_TYPE = Asset.AssetType.OTHER

UNITS_TYPES = {Asset.AssetType.MUTUAL_FUND, Asset.AssetType.STOCK,
               Asset.AssetType.CRYPTO, Asset.AssetType.SGB, Asset.AssetType.GOLD}

DATE_RE = re.compile(
    r"\b(\d{1,2}[/\-.]\d{1,2}[/\-.]\d{2,4}|\d{1,2}[ -][A-Za-z]{3}[ -]\d{2,4}|\d{4}-\d{2}-\d{2})\b")
MONEY_RE = re.compile(r"(?:₹|Rs\.?|INR)?\s*\(?-?[0-9][0-9,]*(?:\.[0-9]{1,2})?\)?")


@dataclass
class DraftRow:
    name: str
    asset_type: str
    date: str
    kind: str          # buy | sell | dividend
    quantity: str      # "" when amount-only
    price: str
    amount: str


@dataclass
class SlipDraft:
    doc_type: str
    asset_type: str
    rows: list = field(default_factory=list)
    raw_text: str = ""
    notes: list = field(default_factory=list)
    closing_balance: str | None = None  # "balance" lines become the holding's value

    @property
    def asset_type_label(self) -> str:
        return dict(Asset.AssetType.choices).get(self.asset_type, self.asset_type.title())


def guess_type(hint: str) -> str:
    low = (hint or "").lower()
    for kw, t in HINT_TYPES:
        if kw in low:
            return t
    return DEFAULT_TYPE


def _parse_date(text: str) -> str:
    m = DATE_RE.search(text)
    if not m:
        return date.today().isoformat()
    raw = m.group(1)
    for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%d/%m/%y", "%d-%m-%y",
                "%d %b %Y", "%d-%b-%Y", "%d %b %y", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue
    return date.today().isoformat()


def _parse_money(text: str) -> Decimal | None:
    cleaned = re.sub(r"(?i)\bcr\b\.?|\bdr\b\.?", "", text)
    cleaned = cleaned.replace("(", "").replace(")", "").replace(",", "").replace("₹", "").strip()
    if not re.match(r"^-?\d*\.?\d+$", cleaned):
        return None
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def ocr_image(path: str) -> str:
    """Read an image with tesseract after a light grayscale preprocessing."""
    img = Image.open(path).convert("L")
    # Upscale small phone photos — tesseract reads ~30px tall digits best
    if img.width < 1200:
        img = img.resize((img.width * 2, img.height * 2))
    return pytesseract.image_to_string(img, config="--psm 6")


def parse_slip(text: str, hint: str) -> SlipDraft:
    asset_type = guess_type(hint)
    draft = SlipDraft(doc_type=hint, asset_type=asset_type, raw_text=text)
    units = asset_type in UNITS_TYPES

    for line in text.splitlines():
        line = line.strip()
        if len(line) < 4 or not DATE_RE.search(line) and not MONEY_RE.search(line):
            continue
        # A "balance" line is the account's current value, not a transaction
        if re.search(r"(?i)closing balance|a\/c balance|account balance|^balance", line):
            m = MONEY_RE.search(line)
            if m:
                v = _parse_money(m.group(0))
                if v is not None:
                    draft.closing_balance = str(v)
            continue
        # A row needs a date or at least one amount; numbers come after text
        numbers = [m for m in MONEY_RE.finditer(line)]
        values = [v for m in numbers if (v := _parse_money(m.group(0))) is not None
                  and v != 0]
        if not values:
            continue
        day = _parse_date(line)
        # Row name: the words before the first number
        first_num = numbers[0].start() if numbers else len(line)
        name = re.sub(r"[|_]{2,}", " ", line[:first_num]).strip(" .:|-–")[:60]
        name = re.sub(r"\s{2,}", " ", name)
        if not name:
            name = f"{asset_type.title()} entry"

        if len(values) >= 2 and units:
            qty, price = values[0], values[-1]
            draft.rows.append(DraftRow(
                name=name, asset_type=asset_type, date=day, kind="buy",
                quantity=str(qty), price=str(price), amount=str(qty * price)))
        else:
            # Single amount: deposit / dividend / withdrawal — user picks
            amount = values[-1]
            kind = "dividend" if re.search(r"(?i)dividend|idcw|int\.? payout", line) else "buy"
            draft.rows.append(DraftRow(
                name=name, asset_type=asset_type, date=day, kind=kind,
                quantity="", price="", amount=str(amount)))

    if not draft.rows:
        draft.notes.append(
            "Could not draft any rows from this image — check the hint text, "
            "or add the entries manually below."
        )
        draft.rows = [DraftRow(name="", asset_type=asset_type, date=date.today().isoformat(),
                               kind="buy", quantity="", price="", amount="")]
    return draft
