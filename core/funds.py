"""Mutual fund explorer: search AMFI schemes and inspect a scheme's NAV history.

Two-tier data:
- Tier A (analysis): local full-history Parquet (data/amfi_nav_master.parquet,
  37.6M rows, all AMFI schemes incl. matured, Apr 2006 → today) queried in place
  with DuckDB — see core/nav_history.py. Refresh weekly by re-downloading.
- Tier B (live): mfapi.in per-scheme API, used for scheme search, dividend
  payouts (Parquet carries NAV only), and as fallback when the local file lacks
  a scheme. Responses are cached on disk under data/nav_cache.
"""
import csv
import json
import os
import re
from datetime import date, datetime, timedelta

import requests

from core import nav_history

CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "nav_cache")
CACHE_TTL_HOURS = 12


def _cached(key: str):
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = os.path.join(CACHE_DIR, f"mf_{key}.json")
    if os.path.exists(path):
        age_hours = (datetime.now().timestamp() - os.path.getmtime(path)) / 3600
        if age_hours < CACHE_TTL_HOURS:
            with open(path) as f:
                return json.load(f)
    return None


def _store(key: str, payload):
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = os.path.join(CACHE_DIR, f"mf_{key}.json")
    with open(path, "w") as f:
        json.dump(payload, f)


# ---------------------------------------------------------------------------
# Browse section: full scheme catalog (owned by the fund-analytics worker)
# ---------------------------------------------------------------------------
CATALOG_CSV = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "schemes_catalog.csv")
CATALOG_MAX_AGE_DAYS = 7
ACTIVE_WINDOW_DAYS = 30  # last NAV within this many days of the latest file date → live scheme

_catalog_mem: list[dict] | None = None
_catalog_mtime: float = 0.0

# Canonical AMC names, longest first so "ICICI Prudential" wins over "ICICI".
_AMCS = sorted([
    "360 ONE", "Aditya Birla Sun Life", "Angel One", "Axis", "Bajaj Finserv", "Bandhan",
    "Bank of India", "Baroda BNP Paribas", "Baroda", "Benchmark", "BNP Paribas", "BOI AXA",
    "Canara Robeco", "Daiwa", "DBS", "Deutsche", "DSP BlackRock", "DSP", "DWS", "Edelweiss",
    "Escorts", "Essel", "Fidelity", "Franklin India", "Franklin Templeton", "Goldman Sachs",
    "Groww", "HDFC", "Helios", "HSBC", "ICICI Prudential", "IDBI", "IDFC", "IIFL", "IL&FS",
    "Indiabulls", "ING", "Invesco India", "ITI", "JM Financial", "JPMorgan", "Kotak", "L&T",
    "LIC MF", "LIC", "Mahindra Manulife", "Mahindra", "Mirae Asset", "Morgan Stanley",
    "Motilal Oswal", "Navi", "Nippon India", "NJ", "Old Bridge", "Parag Parikh", "PGIM India",
    "Pramerica", "Peerless", "Principal", "Quant", "Quantum", "Reliance", "Religare Invesco",
    "Religare", "Sahara", "Samco", "SBI", "Shriram", "Sundaram", "Tata", "Taurus", "Templeton",
    "Trust", "Union KBC", "Union", "UTI", "WhiteOak Capital", "Zerodha", "Unifi", "Capitalmind",
    "Jio BlackRock", "Choice", "The Wealth Company",
], key=len, reverse=True)

# (substring/regex, group, category) in priority order — first hit wins.
_CATEGORY_RULES: list[tuple[str, str, str]] = [
    (r"\bgold\b", "Gold", "Gold"),
    (r"\bfof\b|fund of funds?|fund-of-fund|feeder|\bfund of fund", "FoF", "Fund of Funds"),
    (r"overnight", "Debt", "Overnight"),
    (r"\bliquid\b", "Debt", "Liquid"),
    (r"money market|cash management|cash fund|\bmmf\b", "Debt", "Money Market"),
    (r"\bgilt\b|g-sec|gsec|\bsdl\b|target maturity|bharat bond|crisil ibx", "Debt", "Gilt / Target Maturity"),
    (r"corporate bond", "Debt", "Corporate Bond"),
    (r"credit risk|credit opportunit", "Debt", "Credit Risk"),
    (r"banking (and|&) psu|bank (and|&) psu|banking & public", "Debt", "Banking & PSU"),
    (r"floater|floating", "Debt", "Floater"),
    (r"\bfmp\b|fixed maturity|fixed horizon|\bftif\b|interval|debt fund series|capital protection|"
     r"\bfixed term\b|\bdual advantage\b", "Debt", "FMP / Closed-End"),
    (r"ultra short", "Debt", "Ultra Short Duration"),
    (r"low duration", "Debt", "Low Duration"),
    (r"short duration|short term (debt|bond|income|plan|fund)|short term$|\bstp\b", "Debt", "Short Duration"),
    (r"medium (to long )?duration|medium term|medium to long", "Debt", "Medium Duration"),
    (r"long duration", "Debt", "Long Duration"),
    (r"dynamic bond|dynamic term", "Debt", "Dynamic Bond"),
    (r"\belss\b|tax saver|tax relief|taxsaver|tax plan|long term equity|\btax\b", "Equity", "ELSS"),
    (r"arbitrage", "Equity", "Arbitrage"),
    (r"multi asset", "Equity", "Multi Asset"),
    (r"balanced advantage|dynamic asset|asset allocat", "Equity", "Balanced Advantage"),
    (r"conservative hybrid|monthly income|\bmip\b|regular savings|debt hybrid", "Equity", "Conservative Hybrid"),
    (r"equity savings", "Equity", "Equity Savings"),
    (r"aggressive hybrid|balanced|hybrid|equity & debt|equity and debt", "Equity", "Aggressive Hybrid"),
    (r"\bindex\b|\betf\b|nifty|sensex|bees\b", "Equity", "Index / ETF"),
    (r"large (&|and) ?mid", "Equity", "Large & Mid Cap"),
    (r"large ?cap|bluechip|blue chip|top 100|top 200|frontline", "Equity", "Large Cap"),
    (r"mid ?cap|emerging", "Equity", "Mid Cap"),
    (r"small ?cap", "Equity", "Small Cap"),
    (r"flexi ?cap|flexicap", "Equity", "Flexi Cap"),
    (r"multi ?cap", "Equity", "Multi Cap"),
    (r"focused|focus", "Equity", "Focused"),
    (r"value|contra|dividend yield", "Equity", "Value / Contra"),
    (r"infra|tech|digital|pharma|health|bank|financial|consum|fmcg|psu|mnc|energy|power|"
     r"manufactur|business cycle|thematic|sector|auto|realty|esg|innovation|transport|"
     r"commodit|natural resources|international|global|overseas|us equity|nasdaq|world|services|"
     r"defence|\bipo\b|ethical|factor|best-in-class|\bcomma\b|shariah",
     "Equity", "Sectoral / Thematic"),
    (r"long term", "Debt", "Long Duration"),
    (r"income|bond|debt|treasury|savings|duration|gilt|accrual|fixed|maturity|\bcash\b", "Debt", "Debt (Other)"),
    (r"equity|opportunit|growth fund|diversified|vision|prima|advantage|select|dynamic|quant\b|momentum|"
     r"quality|conglomerate|retirement|children|bhavishya|teck|special situation|largecap|midcap",
     "Equity", "Equity (Other)"),
]
_CATEGORY_RES = [(re.compile(p), g, c) for p, g, c in _CATEGORY_RULES]

CATEGORY_GROUPS = ["Equity", "Debt", "Gold", "FoF", "Other"]

# Legacy / variant spellings → canonical AMC (checked before _AMCS).
_AMC_ALIASES = sorted({
    "aditya birla sunlife": "Aditya Birla Sun Life", "birla sun life": "Aditya Birla Sun Life",
    "birla sunlife": "Aditya Birla Sun Life", "birla": "Aditya Birla Sun Life",
    "aditya birla": "Aditya Birla Sun Life", "abn amro": "ABN AMRO", "amro": "ABN AMRO",
    "dhfl pramerica": "DHFL Pramerica", "jm": "JM Financial", "fortis": "Fortis",
    "standard chartered": "Standard Chartered", "licmf": "LIC MF", "pinebridge": "PineBridge",
    "grindlays": "Grindlays", "abakkus": "Abakkus", "jioblackrock": "Jio BlackRock",
    "trustmf": "Trust", "tfmp": "Tata", "bharat bond": "Edelweiss", "ask": "ASK",
    "quant": "Quant", "uti-": "UTI", "idfc-": "IDFC", "sbi-": "SBI",
}.items(), key=lambda kv: len(kv[0]), reverse=True)


def parse_scheme_name(name: str) -> dict:
    """Coarse attributes from an AMFI scheme name: fund_house, plan, payout,
    group, category. Heuristic — AMFI names are free text."""
    clean = re.sub(r"\s+", " ", name or "").strip()
    low = clean.lower()

    low = re.sub(r"^(z-)?redeemed-", "", low)
    known = None
    for alias, canon in _AMC_ALIASES:
        if low.startswith(alias) and (alias.endswith("-") or low[len(alias):len(alias) + 1] in (" ", "-", "")):
            known = (canon, len(alias))
            break
    if known is None:
        for a in _AMCS:
            al = a.lower()
            if low.startswith(al) and low[len(al):len(al) + 1] in (" ", "-", ""):
                known = (a, len(al))
                break
    house = known[0] if known else None
    if house is None:
        head = re.split(r"(?i)\s(?:fund|mf|fof|etf|scheme|plan)\b|\s-|-\s|\(", clean, maxsplit=1)[0]
        house = " ".join(head.split()[:2]) or clean[:20]

    plan = "Direct" if re.search(r"\bdirect\b|-direct|direct-", low) else "Regular"

    low_payout = low.replace("dividend yield", "")
    if "payout" in low_payout:
        payout = "Payout"
    elif re.search(r"idcw|dividend|\bdiv\b|income distribution|bonus|reinvest", low_payout):
        payout = "IDCW"
    else:
        payout = "Growth"

    # Classify on the name minus a known AMC prefix ("Bank of India …" is not a banking fund).
    rest = low[known[1]:] if known else low
    group, category = "Other", "Other"
    for rx, g, c in _CATEGORY_RES:
        if rx.search(rest):
            group, category = g, c
            break
    if category == "Other" and "silver" in low:
        category = "Silver"
    return {"fund_house": house, "plan": plan, "payout": payout, "group": group, "category": category}


def _build_catalog_csv():
    """One DuckDB pass over the NAV Parquet: latest name + first/last NAV date
    per scheme code. ~1s for 38k schemes."""
    if not nav_history._parquet_exists():
        return False
    rows = nav_history._connect().execute(
        "SELECT scheme_code, arg_max(scheme_name, date), min(date), max(date) "
        "FROM read_parquet(?) GROUP BY 1 ORDER BY 2",
        [nav_history.NAV_PARQUET],
    ).fetchall()
    os.makedirs(os.path.dirname(CATALOG_CSV), exist_ok=True)
    tmp = CATALOG_CSV + ".tmp"
    with open(tmp, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["code", "name", "first_date", "last_date"])
        for code, name, d0, d1 in rows:
            w.writerow([code, name or f"Scheme {code}", str(d0), str(d1)])
    os.replace(tmp, CATALOG_CSV)
    return True


def scheme_catalog() -> list[dict]:
    """All schemes with parsed attributes, memoised in-process and on disk
    (data/schemes_catalog.csv, rebuilt weekly or when the Parquet is newer)."""
    global _catalog_mem, _catalog_mtime
    stale = (not os.path.exists(CATALOG_CSV)
             or (datetime.now().timestamp() - os.path.getmtime(CATALOG_CSV)) > CATALOG_MAX_AGE_DAYS * 86400
             or (nav_history._parquet_exists()
                 and os.path.getmtime(nav_history.NAV_PARQUET) > os.path.getmtime(CATALOG_CSV)))
    if stale:
        try:
            _build_catalog_csv()
        except Exception:
            pass
    if not os.path.exists(CATALOG_CSV):
        return []
    mtime = os.path.getmtime(CATALOG_CSV)
    if _catalog_mem is not None and mtime == _catalog_mtime:
        return _catalog_mem
    with open(CATALOG_CSV, newline="") as f:
        raw = list(csv.DictReader(f))
    latest = max((r["last_date"] for r in raw), default="")
    cutoff = ""
    if latest:
        cutoff = (date.fromisoformat(latest) - timedelta(days=ACTIVE_WINDOW_DAYS)).isoformat()
    rows = []
    for r in raw:
        p = parse_scheme_name(r["name"])
        rows.append({"code": int(r["code"]), "name": r["name"], "first_date": r["first_date"],
                     "last_date": r["last_date"], "active": r["last_date"] >= cutoff,
                     "_low": r["name"].lower(), **p})
    _catalog_mem, _catalog_mtime = rows, mtime
    return rows


def filter_catalog(rows, q="", plan="", payout="", group="", category="", include_inactive=False):
    """Filter catalog rows. All query tokens must appear in the name (or q is
    an exact scheme code). Matured/closed schemes are hidden unless asked."""
    tokens = q.lower().split()
    code_q = int(q) if q.strip().isdigit() else None
    plan, payout, group, category = plan.lower(), payout.lower(), group.lower(), category.lower()
    out = []
    for r in rows:
        if code_q is not None:
            if r["code"] != code_q and not all(t in r.get("_low", r["name"].lower()) for t in tokens):
                continue
        elif tokens and not all(t in r.get("_low", r["name"].lower()) for t in tokens):
            continue
        if not include_inactive and not r.get("active", True) and r["code"] != code_q:
            continue
        if plan and r["plan"].lower() != plan:
            continue
        if payout and r["payout"].lower() != payout:
            continue
        if group and r["group"].lower() != group:
            continue
        if category and r.get("category", "").lower() != category:
            continue
        out.append(r)
    return out


BROWSE_PER_PAGE = 50


def browse_context(params) -> dict:
    """Everything the /funds/ browse page needs from request.GET-like params."""
    from django.core.paginator import Paginator
    from urllib.parse import urlencode

    state = {k: (params.get(k) or "").strip() for k in ("q", "plan", "payout", "group", "cat")}
    state["all"] = "1" if params.get("all") == "1" else ""
    rows = scheme_catalog()
    matched = filter_catalog(rows, q=state["q"], plan=state["plan"], payout=state["payout"],
                             group=state["group"], category=state["cat"],
                             include_inactive=bool(state["all"]))
    page = Paginator(matched, BROWSE_PER_PAGE).get_page(params.get("page") or 1)

    def qs(**override):
        merged = {**state, **override}
        return "?" + urlencode({k: v for k, v in merged.items() if v})

    def chips(key, options):
        cur = state[key].lower()
        out = [{"label": "All", "url": qs(**{key: "", "cat": "" if key == "group" else state["cat"]}),
                "active": not cur}]
        for value, label in options:
            extra = {"cat": ""} if key == "group" else {}
            out.append({"label": label, "url": qs(**{key: value, **extra}),
                        "active": cur == value.lower()})
        return out

    categories = []
    if state["group"]:
        seen = {}
        for r in filter_catalog(rows, group=state["group"], include_inactive=bool(state["all"])):
            seen[r["category"]] = seen.get(r["category"], 0) + 1
        categories = [{"label": f"{c} ({n:,})", "url": qs(cat=c), "active": state["cat"].lower() == c.lower()}
                      for c, n in sorted(seen.items())]
    window = [n for n in page.paginator.page_range if abs(n - page.number) <= 2]
    return {
        "state": state,
        "page": page,
        "total": len(matched),
        "universe": len(rows),
        "plan_chips": chips("plan", [("direct", "Direct"), ("regular", "Regular")]),
        "payout_chips": chips("payout", [("growth", "Growth"), ("idcw", "IDCW"), ("payout", "Payout")]),
        "group_chips": chips("group", [(g.lower(), g) for g in CATEGORY_GROUPS]),
        "category_chips": categories,
        "toggle_all_url": qs(all="" if state["all"] else "1", page=""),
        "page_urls": [{"n": n, "url": qs(page=str(n)), "current": n == page.number} for n in window],
        "prev_url": qs(page=str(page.previous_page_number())) if page.has_previous() else "",
        "next_url": qs(page=str(page.next_page_number())) if page.has_next() else "",
        "first_url": qs(page="1"),
        "last_url": qs(page=str(page.paginator.num_pages)),
    }


def search_funds(q: str) -> list[dict]:
    """Scheme search: [{code, name}] — local catalog first (live + matured, no
    network), then the mfapi live universe. Empty list on failure."""
    q = q.strip()
    if len(q) < 3:
        return []
    try:
        hits = filter_catalog(scheme_catalog(), q=q)[:25]
        if hits:
            return [{"code": r["code"], "name": r["name"]} for r in hits]
    except Exception:
        pass
    try:
        r = requests.get(f"https://api.mfapi.in/mf/search?q={q}", timeout=15)
        rows = r.json()
        if isinstance(rows, list) and rows:
            return [{"code": row["schemeCode"], "name": row["schemeName"]} for row in rows[:25]]
    except Exception:
        pass
    return nav_history.local_scheme_names(q)


def _parse_nav_date(s: str) -> date:
    """mfapi dates are dd-mm-YYYY; Parquet dates are ISO."""
    for fmt in ("%d-%m-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"unparseable NAV date: {s!r}")


def scheme_detail(code: int) -> dict | None:
    """Full scheme view: meta, NAV series (downsampled), trailing returns, and
    dividend payouts when the feed publishes them for this scheme."""
    cached = _cached(str(code))
    if cached:
        return cached

    series = nav_history.local_nav_history(code)  # [(iso-date, nav)] ascending
    meta: dict = {}
    dividends: list[dict] = []
    if series:
        meta = nav_history.local_scheme_meta(code)
    else:
        try:
            r = requests.get(f"https://api.mfapi.in/mf/{code}", timeout=20)
            raw = r.json()
        except Exception:
            return None
        meta = raw.get("meta") or {}
        series = [(d["date"], float(d["nav"])) for d in raw.get("data", []) if d.get("nav")]
        dividends = [
            {"date": d["date"], "per_unit": float(d["div"]), "nav": float(d["nav"])}
            for d in raw.get("data", [])
            if float(d.get("div") or 0) > 0
        ]
        dividends.sort(key=lambda x: _parse_nav_date(x["date"]))

    if not series:
        return None
    # Chronological order for return math.
    series.sort(key=lambda t: _parse_nav_date(t[0]))
    latest_date = _parse_nav_date(series[-1][0])
    latest_nav = series[-1][1]

    def trailing_return(years: float) -> float | None:
        target = latest_date - timedelta(days=round(years * 365.25))
        past = next((nav for d, nav in series if _parse_nav_date(d) >= target), None)
        if past is None or past == 0:
            return None
        return round((latest_nav / past - 1) * 100, 2)

    start = meta.get("scheme_start_date")
    out = {
        "code": code,
        "name": meta.get("scheme_name", f"Scheme {code}"),
        "fund_house": meta.get("fund_house", ""),
        "category": meta.get("scheme_category", ""),
        "type": meta.get("scheme_type", ""),
        "isin": meta.get("isin_growth") or meta.get("isin_div_reinvestment") or "",
        "inception": (start or {}).get("date") if isinstance(start, dict) else (start or ""),
        "latest_nav": latest_nav,
        "latest_date": latest_date.isoformat(),
        "returns": {k: trailing_return(y) for k, y in
                    [("1M", 1/12), ("6M", 0.5), ("1Y", 1), ("3Y", 3), ("5Y", 5)]},
        # Full daily series; the chart samples with LTTB and zooms into it.
        "chart": [{"d": _parse_nav_date(d).isoformat(), "nav": nav} for d, nav in series],
        "dividends": dividends,
        "vr_link": f"https://www.valueresearchonline.com/funds/news-worth-tracking/?q={code}",
    }
    _store(str(code), out)
    return out
