"""Goal planning engine — a faithful port of the Freefincal robo-advisor workbook
(July 2025) goal math: one-time goals, recurring goals, and the low-stress
retirement estimate. Pure functions; no Django dependencies.

Workbook conventions preserved:
- Existing corpus grows at its own post-tax rate (equity / taxable FI / tax-free FI);
  for one-time goals only from the SIP start year to the goal (Step 4A B19).
- Goal cost inflates at education-style inflation (default 10%, overridable).
- Net corpus = inflated cost − FV of earmarked investments − expected lumpsums.
- Monthly SIP solves for net corpus with an annual step-up (default 5%) on the
  workbook's ANNUAL schedule: a year's 12 instalments accumulate uninvested and
  the whole balance compounds once at that year's return (Step 3 N3, Step 4A
  J4). One-time goals earn a glide-path return that de-risks year by year
  toward the goal (Settings rows 45–48).
- Recurring goals: each payment has its own growing sinking fund (Step 4B G).
- Retirement: expenses that persist inflate at 6% to retirement; the corpus is
  the workbook's present-value sum over the 4-bucket ladder (income / low /
  medium / high risk): each year laddered inside its bucket, each bucket
  discounted at its glide-path return until it starts (low stress buckets
  M6/R6/W6/AB6). Plus a 5% emergency corpus (F13).
- Post-retirement income (rent, pension…) and an optional flat annuity floor
  (income flooring L51/F12) reduce what the investment corpus must fund.
"""
import math
from dataclasses import dataclass, field


@dataclass
class Assumptions:
    inflation: float = 6.0            # Settings!C3
    inflation_retirement: float = 6.0 # Settings!C5
    equity_return: float = 10.0       # Settings!C7 (post-tax)
    fixed_return: float = 6.0         # Settings!C8 − 1% (taxable, post-tax)
    taxfree_return: float = 7.0       # Settings!C8 (EPF/PPF)
    goal_inflation: float = 10.0      # education/lifestyle inflation
    life_expectancy: int = 90         # younger spouse
    spouse_age: int | None = None     # Step 1 F8; None = single / not given
    annuity_rate: float = 4.0         # Settings!H7: prices the income floor
    ladder_return: float = 5.0        # Settings!B14:B16: ladder return inside each risk bucket
    recurring_return: float = 7.0     # Settings!C37: recurring-goal RD / liquid fund return


def _blended(equity_w: float, eq_ret: float, fi_ret: float) -> float:
    return equity_w * eq_ret + (1 - equity_w) * fi_ret


def fv(amount: float, rate_pct: float, years: float) -> float:
    return amount * (1 + rate_pct / 100) ** years


@dataclass
class GoalResult:
    future_cost: float
    fv_current: float
    net_corpus: float
    lumpsum_now: float
    monthly_sip: float
    step_up_pct: float
    equity_alloc_pct: float
    schedule: list = field(default_factory=list)  # [(year, monthly_amt, year_end_value)]
    # lumpsum_now is the one-time amount that, invested today in the goal portfolio,
    # alone reaches net_corpus: an alternative to the SIP, not a part of it.
    pre_goal_return: float = 0.0       # % return of the goal portfolio while accumulating
    post_goal_return: float | None = None  # % return while the corpus is being spent (recurring)
    derisk_years: float = 0.0          # final years before the goal held entirely in fixed income
    years_to_goal: float = 0.0         # years from today to the goal (recurring: to the first payment)
    years_to_start: float = 0.0        # years from today until the SIP begins
    # one-time goals: equity weight and portfolio return (%) for each SIP year (glide path)
    glide_path: list = field(default_factory=list)
    year_returns: list = field(default_factory=list)
    # Contribution timing of the SIP schedule: True = each year's 12 instalments
    # earn that year's return (Step 3 / 4A); False = credited at year end
    # (Step 4B sinking funds with typegr1 = 0).
    sip_due: bool = True
    # recurring goals: one sinking fund per payment (Step 4B rows 5..):
    # [{year, amount, net, monthly_sip, invest_from, invest_years}]
    occurrences: list = field(default_factory=list)


def glide_ends(flexible: bool, years: float) -> tuple[float, float]:
    """(start, end) equity weight of the one-time goal glide path, where years
    is the SIP window (years to goal − years until the SIP starts).
    Settings!E45 (start) and G45 (end):
      rigid:    0 / 10 / 30 / 50 / 60% for ≤5 / ≤7 / ≤10 / ≤15 / >15 years, ending at 0%
      flexible: 10 / 20 / 40 / 60 / 70% for the same bands, ending at 10% (≤5 years) or 20%."""
    n = years
    if flexible:
        start = 0.1 if n <= 5 else 0.2 if n <= 7 else 0.4 if n <= 10 else 0.6 if n <= 15 else 0.7
        return start, (0.1 if n <= 5 else 0.2)
    start = 0.0 if n <= 5 else 0.1 if n <= 7 else 0.3 if n <= 10 else 0.5 if n <= 15 else 0.6
    return start, 0.0


def glide_path(flexible: bool, years: int) -> list[float]:
    """Equity weight for each SIP year 1..years (workbook Step 4A column F).

    Settings rows 45–48: hold the start weight E45 for segment 1
    (C46 = ROUND(50%·N) if N > 20 else ROUND(40%·N), at least 1), then fall to
    the midpoint D47 = (E45 + G45)/2 in equal steps over segment 2
    (C47 = ROUND(30%·N), at least 1), then to the end weight G45 in equal steps
    over segment 3 (C48 = N − C46 − C47, at least 0)."""
    n = max(0, int(years))
    start, end = glide_ends(flexible, n)
    return _glide_weights(n, start, end)


def _glide_weights(years: int, start: float, end: float) -> list[float]:
    """The workbook's 3-segment glide from `start` to `end` equity weight over
    `years` (Settings rows 45–48, and rows 132–149 for the retirement buckets)."""
    n = max(0, int(years))
    seg1 = _xround(0.5 * n) if n > 20 else max(1, _xround(0.4 * n))
    seg2 = max(1, _xround(0.3 * n))
    seg3 = max(0, n - seg1 - seg2)
    mid = (start + end) / 2
    step2, step3 = (start - mid) / seg2, (mid - end) / (seg3 or 1)
    out: list[float] = []
    for e in range(1, n + 1):
        if start == end or e <= seg1:
            w = start
        elif e <= seg1 + seg2:
            w = out[-1] - step2
        else:
            w = out[-1] - step3
        out.append(max(0.0, w))
    return out


def _annual_rates(year_returns_pct: list[float], derisk_months: int = 0,
                  derisk_return_pct: float | None = None) -> list[float]:
    """One effective annual rate (fraction) per SIP year from one return (%)
    per year, with the final derisk_months at the fixed-income return. A year
    split between the two compounds each part for its share of the year."""
    n = len(year_returns_pct)
    d = int(derisk_months) if derisk_months and derisk_return_pct is not None else 0
    cut = max(0, n * 12 - d)
    out = []
    for y, r in enumerate(year_returns_pct):
        fi_m = min(12, max(0, (y + 1) * 12 - cut))
        g = (1 + r / 100) ** ((12 - fi_m) / 12)
        if fi_m:
            g *= (1 + derisk_return_pct / 100) ** (fi_m / 12)
        out.append(g - 1)
    return out


def lumpsum_growth(annual_return_pct: float, months: int, derisk_months: int = 0,
                   derisk_return_pct: float | None = None) -> float:
    """Growth factor of ₹1 invested now over `months`, compounded annually as
    the workbook's lump sum is (Step 4A N3 = M3·(1+H3), N4 = N3·(1+H4)), with
    an optional fixed-income tail."""
    months = max(0, int(months))
    d = min(months, int(derisk_months)) if derisk_months and derisk_return_pct is not None else 0
    g = (1 + annual_return_pct / 100) ** ((months - d) / 12)
    if d:
        g *= (1 + derisk_return_pct / 100) ** (d / 12)
    return g


def _fv_existing(equity: float, taxable_fi: float, taxfree_fi: float, years: float,
                 a: "Assumptions", derisk_years: float = 0.0) -> float:
    """Earmarked investments compounded over `years`, each at its own post-tax rate.
    One-time goals pass the SIP window (years to goal − years until the SIP
    starts), as the workbook's Step 4A B19 does. De-risking moves the equity
    part to taxable fixed income for the final years."""
    d = min(max(0.0, derisk_years), max(0.0, years))
    eq = fv(fv(equity, a.equity_return, years - d), a.fixed_return, d)
    return eq + fv(taxable_fi, a.fixed_return, years) + fv(taxfree_fi, a.taxfree_return, years)


def solve_sip(net_corpus: float, annual_return_pct: float, months: int,
              step_up_pct: float, derisk_months: int = 0,
              derisk_return_pct: float | None = None,
              rates: list[float] | None = None, due: bool = True) -> tuple[float, list]:
    """Monthly SIP that grows step_up% each year and reaches net_corpus, on the
    workbook's ANNUAL schedule (Step 3 N3 = (12·M3 + N2)·(1 + L3), Step 4A
    J4 = (12·I4 + J3)·(1 + H4)): a year's 12 instalments accumulate uninvested,
    then the whole balance compounds once at that year's return. months is
    taken in whole years (the workbook plans in years). With derisk_months,
    the portfolio earns derisk_return_pct (fixed income) for the last
    derisk_months instead. rates (one effective annual rate, as a fraction,
    per SIP year) overrides the single return, e.g. a glide path.
    due=False credits each year's instalments at year end without that year's
    growth (Step 4B sinking funds, typegr1 = 0).
    Returns (sip, [(year, monthly_amount, year_end_value)])."""
    years = int(round(max(0, months) / 12))
    if years <= 0:
        return 0.0, []
    if rates is None:
        rates = _annual_rates([annual_return_pct] * years, derisk_months, derisk_return_pct)
    step = 1 + step_up_pct / 100

    def path(first_month: float) -> list[tuple]:
        out, bal = [], 0.0
        for y in range(years):
            m = first_month * step ** y
            bal = (bal + 12 * m) * (1 + rates[y]) if due else bal * (1 + rates[y]) + 12 * m
            out.append((y + 1, m, bal))
        return out

    # The end value is linear in the SIP, so solve it directly.
    unit = path(1.0)[-1][2]
    sip = max(0.0, net_corpus) / unit if unit > 0 else 0.0
    schedule = [(y, m, round(b)) for y, m, b in path(sip)]
    return round(sip, 0), schedule


def one_time_goal(*, cost_today: float, years_to_goal: float, years_to_start: float = 0.0,
                  flexible: bool = False, inflation_override: float | None = None,
                  current_equity: float = 0.0, current_taxable_fi: float = 0.0,
                  current_taxfree_fi: float = 0.0, lumpsum_expected: float = 0.0,
                  step_up_pct: float = 5.0, derisk_years: float = 0.0,
                  a: Assumptions | None = None) -> GoalResult:
    """Step 4A block.

    - Glide path: the goal portfolio's equity share follows the workbook's
      schedule over the SIP window (glide_path), and each SIP year earns
      equity_return × w + fixed_return × (1 − w) for that year's weight w
      (Step 4A H = F·Settings!C40 + G·C41). equity_alloc_pct is the starting
      weight; glide_path / year_returns hold the whole schedule.
    - Earmarked investments compound at their own post-tax rates only from the
      SIP start to the goal, years_to_goal − years_to_start, as the workbook
      does (Step 4A B19 = value × (1 + r)^(B4 − B5)).
    - derisk_years > 0: the whole goal portfolio (SIP and earmarked equity) sits
      in taxable fixed income for the final derisk_years — Freefincal's "move
      the money to safety before the goal" rule, overriding the glide path
      there. 0 keeps the workbook's glide path to the end.
    - lumpsum_now is the one-time amount that alone reaches the net corpus, an
      alternative to the SIP. Like the workbook's lump sum (Step 4A M3, grown
      down column N) it is credited with growth along the glide path over the
      SIP window only, the same window as earmarked investments.
    """
    a = a or Assumptions()
    infl = inflation_override if inflation_override else a.goal_inflation
    years = max(0.0, float(years_to_goal))
    start = min(max(0.0, float(years_to_start)), years)
    window = years - start                       # workbook B4 − B5
    d = min(max(0.0, float(derisk_years)), window)
    future_cost = max(0.0, fv(cost_today, infl, years))
    fv_current = _fv_existing(current_equity, current_taxable_fi, current_taxfree_fi, window, a, d)
    net = max(0.0, future_cost - fv_current - lumpsum_expected)
    sip_years = int(round(window))
    path = glide_path(flexible, sip_years)
    year_returns = [round(_blended(w, a.equity_return, a.fixed_return), 6) for w in path]
    months = sip_years * 12
    derisk_m = min(months, int(round(d * 12)))
    rates = _annual_rates(year_returns, derisk_m, a.fixed_return)
    schedule: list = []
    if months > 0:
        sip, schedule = solve_sip(net, 0.0, months, step_up_pct, rates=rates)
        lumpsum = net / math.prod(1 + r for r in rates)
        # Average glide-path return (de-risking excluded): the single annual
        # rate that, compounded yearly, grows ₹1 as much as the path.
        glide = math.prod(1 + r for r in _annual_rates(year_returns))
        ret = round((glide ** (1 / sip_years) - 1) * 100, 4)
    else:
        sip, lumpsum = 0.0, net  # no SIP window left: only money already set aside can fund it
        ret = _blended(glide_ends(flexible, 0)[0], a.equity_return, a.fixed_return)
    eq_start = glide_ends(flexible, sip_years)[0]
    return GoalResult(future_cost=round(future_cost), fv_current=round(fv_current),
                      net_corpus=round(net), lumpsum_now=round(max(0.0, lumpsum)),
                      monthly_sip=sip, step_up_pct=step_up_pct,
                      equity_alloc_pct=round(eq_start * 100), schedule=schedule,
                      pre_goal_return=ret, post_goal_return=None, derisk_years=d,
                      years_to_goal=years, years_to_start=float(years_to_start),
                      glide_path=path, year_returns=year_returns)


def recurring_goal(*, annual_cost_today: float, years_until_start: float, years_duration: float,
                   flexible: bool = False, inflation_override: float | None = None,
                   current_equity: float = 0.0, current_taxable_fi: float = 0.0,
                   current_taxfree_fi: float = 0.0, step_up_pct: float = 5.0,
                   derisk_years: float = 0.0, post_goal_return: float | None = None,
                   every_years: int = 1, a: Assumptions | None = None) -> GoalResult:
    """Step 4B: every payment of a recurring goal gets its own growing sinking
    fund in an RD / liquid fund ("for all these goals, you use a simple RD or
    sweep in SB acct or liquid fund").

    Payment i (0-based) falls in year B4 + i·B5 (years_until_start +
    i·every_years), years_duration (B6) times, and costs
    F = cost·(1 + inflation)^year. Its monthly SIP, stepped up step_up% a year,
    is G = F·(r − g) / (12·((1 + r)^n − (1 + g)^n)) with r = the recurring
    return (Settings!C37, 7%), g = the step-up, and n = B4 for the first
    payment and B5 after that: the fund for the next payment starts the year
    the previous one is paid, so investing continues through the whole goal
    period. Instalments are credited at year end (typegr1 = 0).

    Earmarked investments grow to the first payment at their own rates (as in
    one_time_goal) and pay the earliest payments first; any excess keeps
    earning r. post_goal_return overrides r. future_cost / net_corpus /
    lumpsum_now refer to the first payment (what must be in hand at the goal
    start); monthly_sip is the SIP to start today; occurrences and schedule
    hold the whole plan. flexible is unused: the money is never in equity."""
    a = a or Assumptions()
    infl = (inflation_override if inflation_override else a.goal_inflation) / 100
    ret_pct = a.recurring_return if post_goal_return is None else float(post_goal_return)
    r = ret_pct / 100
    times = max(0, int(round(years_duration)))
    every = max(1, int(round(every_years)))
    first = max(0, int(round(years_until_start)))
    y = float(first)
    d = min(max(0.0, float(derisk_years)), y)
    fv_current = _fv_existing(current_equity, current_taxable_fi, current_taxfree_fi, y, a, d)
    credit, occurrences, schedule = fv_current, [], []
    for i in range(times):
        year = first + i * every
        n = first if i == 0 else every
        if i:
            credit *= (1 + r) ** every
        amount = annual_cost_today * (1 + infl) ** year
        use = min(credit, amount)
        credit -= use
        need = amount - use
        sip, sched = solve_sip(need, ret_pct, n * 12, step_up_pct, due=False)
        occurrences.append({"year": year, "amount": round(amount), "net": round(need),
                            "monthly_sip": sip, "invest_from": year - n, "invest_years": n})
        schedule += [(year - n + k, m, v) for k, m, v in sched]
    head = occurrences[0] if occurrences else {"amount": 0, "net": 0}
    future_cost, net = float(head["amount"]), float(head["net"])
    monthly_sip = next((o["monthly_sip"] for o in occurrences
                        if o["invest_from"] == 0 and o["invest_years"] > 0), 0.0)
    lumpsum = net / (1 + r) ** first
    return GoalResult(future_cost=round(future_cost), fv_current=round(fv_current),
                      net_corpus=round(net), lumpsum_now=round(max(0.0, lumpsum)),
                      monthly_sip=monthly_sip, step_up_pct=step_up_pct,
                      equity_alloc_pct=0, schedule=schedule,
                      pre_goal_return=ret_pct, post_goal_return=ret_pct, derisk_years=d,
                      years_to_goal=y, years_to_start=0.0, sip_due=False,
                      occurrences=occurrences)


def goal_scenarios(*, kind: str, a: Assumptions | None = None, **kwargs) -> list[dict]:
    """What-if table for a goal: market regimes and decision changes.

    Bear  : equity 7%, FI 5%, inflation 7% everywhere
    Base  : workbook assumptions
    Bull  : equity 12%, FI 7%, inflation 5%
    Decision variants on the base case: 10% step-up instead of the plan's,
    (retirement only) retiring 3 years later, and (one-time / recurring) de-risking:
    the goal money sits in fixed income for the last 3 years before the goal
    (recurring money is already all fixed income, so de-risking is a no-op there).
    Returns [{label, monthly_sip, net_corpus, note}].
    """
    a = a or Assumptions()
    variants = [
        ("Bear", Assumptions(inflation=7, inflation_retirement=7, equity_return=7,
                             fixed_return=5, taxfree_return=6, goal_inflation=12,
                             recurring_return=6, annuity_rate=a.annuity_rate,
                             ladder_return=a.ladder_return,
                             life_expectancy=a.life_expectancy, spouse_age=a.spouse_age), kwargs),
        ("Base", a, kwargs),
        ("Bull", Assumptions(inflation=5, inflation_retirement=5, equity_return=12,
                             fixed_return=7, taxfree_return=7.5, goal_inflation=8,
                             recurring_return=7.5, annuity_rate=a.annuity_rate,
                             ladder_return=a.ladder_return,
                             life_expectancy=a.life_expectancy, spouse_age=a.spouse_age), kwargs),
    ]
    step = float(kwargs.get("step_up_pct", 5))
    variants.append(("10% step-up", a, {**kwargs, "step_up_pct": 10}))
    if kind == "retirement":
        late = {**kwargs, "retire_age": int(kwargs.get("retire_age", 50)) + 3}
        variants.append((f"Retire at {late['retire_age']}", a, late))
    else:
        variants.append(("De-risk 3y", a, {**kwargs, "derisk_years": 3}))

    out = []
    for label, asm, kw in variants:
        kw = {k: v for k, v in kw.items() if k not in ("step_up_pct",)}
        step_up = 10 if label == "10% step-up" else step
        if kind == "one_time":
            r = one_time_goal(a=asm, step_up_pct=step_up, **kw)
            note = (f"all fixed income for the last {r.derisk_years:g} years" if r.derisk_years
                    else glide_text(r))
        elif kind == "recurring":
            r = recurring_goal(a=asm, step_up_pct=step_up, **kw)
            if label == "De-risk 3y":
                note = "already all RD / liquid fund" + (
                    f"; earmarked equity in fixed income for the last {r.derisk_years:g} years"
                    if r.derisk_years else "")
            else:
                note = f"equity {r.equity_alloc_pct}%"
        else:
            r = retirement_plan(a=asm, step_up_pct=step_up, **kw)
            note = ("on track" if r.on_track
                    else f"post-ret equity {r.post_ret_equity_pct}%")
        out.append({"label": label, "monthly_sip": r.monthly_sip,
                    "net_corpus": r.net_corpus, "note": note})
    return out


def glide_text(r: "GoalResult") -> str:
    """'equity 40% → 20%' for a glide path, 'equity 0%' when it stays flat."""
    path = r.glide_path or []
    end = round(path[-1] * 100) if path else r.equity_alloc_pct
    return (f"equity {r.equity_alloc_pct}% → {end}%" if end != r.equity_alloc_pct
            else f"equity {r.equity_alloc_pct}%")


# ---------------------------------------------------------------- retirement

def _xround(x: float) -> int:
    """Excel ROUND(x, 0): half away from zero (Python's round() is banker's)."""
    return int(math.floor(abs(x) + 0.5)) * (1 if x >= 0 else -1)


def planning_period(current_age: float, retire_age: float, a: "Assumptions") -> int:
    """Years the retirement corpus must last (Settings!C10):
    C4 − MAX(current age, retire age) + (older − younger spouse age if married).
    The workbook asks for the older spouse's age as "your age" and life
    expectancy for the younger spouse, so a couple's plan runs the age gap
    longer. Here current_age is the user's own age and a.spouse_age the
    spouse's, so the gap is their absolute difference."""
    n = int(a.life_expectancy - max(current_age, retire_age))
    if a.spouse_age is not None:
        n += abs(int(current_age) - int(a.spouse_age))
    return max(0, n)


@dataclass
class Bucket:
    name: str
    years: int
    return_pct: float      # growth before the bucket is drawn (low stress buckets F17:F19)
    equity_pct: int        # bucket mix (Settings!C19:C22), used for the equity share
    short: str = ""
    ladder_pct: float = 0.0  # ladder return inside the bucket (Settings!B13:B16)
    start: int = 0           # retirement year (0-based) the bucket starts paying


def _bucket_start_equity(years: int, cap: float, low_bucket: bool = False) -> float:
    """Start equity of a risk bucket's glide (Settings!E132 / E139 / E146,
    non-flexible): 0 / 10 / 30 / 50% for ≤5 / ≤7 / ≤10 / ≤15 years until the
    bucket starts, else the bucket's mix C21 / C22.

    Workbook bug, not copied: the low bucket's E146 uses 40% in the ≤15 band
    and only falls back to C20 above 15 years, but the low bucket always
    starts within 15 years (E13 = MIN(C10, 15)), so its C20 branch is dead and
    a short plan's low bucket (C20 = 20%) would glide from 40%, above its own
    mix. Intended rule here: the 40% band capped at C20. With the workbook
    defaults (C20 ≥ 40% for T > 25) this equals the workbook's 40%."""
    n = years
    w = 0.0 if n <= 5 else 0.1 if n <= 7 else 0.3 if n <= 10 else None
    if low_bucket:
        return min(cap, 0.4 if w is None else w)
    return (0.5 if n <= 15 else cap) if w is None else w


def bucket_glide_return(years_to_start: int, start_equity: float,
                        a: Assumptions | None = None) -> float:
    """Geometric average annual return (fraction, unrounded) of a risk bucket
    gliding from start_equity to 0% equity over the years until it starts
    ('bucket backend'!T2 / W2 / Z2 = PRODUCT(1 + r_y)^(1/n) − 1, with
    r_y = w·Settings!C7 + (1 − w)·C8)."""
    a = a or Assumptions()
    n = max(0, int(years_to_start))
    if n == 0:
        return a.taxfree_return / 100
    g = math.prod(1 + _blended(w, a.equity_return, a.taxfree_return) / 100
                  for w in _glide_weights(n, start_equity, 0.0))
    return g ** (1 / n) - 1


def bucket_ladder(period: int, a: Assumptions | None = None) -> list[Bucket]:
    """The workbook's 4-bucket ladder (Settings!E13:E16 tenures, C19:C22 mixes).

    Income bucket: first min(T, 15) years, a fixed-income ladder at the
    taxable-FI rate (Settings!B13, 6%). Then Low / Medium / High risk buckets
    whose tenures and equity mixes depend on the total period T. Each risk
    bucket grows, until it starts paying, at its glide-path average return
    rounded to 0.1% (low stress buckets F17:F19 = ROUND('bucket backend'!Z2 /
    W2 / T2, 3), the workbook default B28:B30 = "yes"), and is laddered inside
    at Settings!B14:B16 (5%).

    Workbook bug, not copied: F19 (high) tests B29 instead of B30; with the
    defaults all three toggles are "yes", and the app always uses the glide."""
    a = a or Assumptions()
    t = max(0, int(period))
    income = min(t, 15)
    if _xround(0.4 * t) < 15:
        low = max(0, _xround(0.5 * (t - income)))
        med = max(0, _xround(0.25 * (t - income)))
    else:
        low, med = _xround(0.25 * t), _xround(0.2 * t)
    high = max(0, t - income - low - med)
    if t <= 25:
        eq = (0.2, 0.3, 0.7)
    elif t <= 35:
        eq = (0.4, 0.5, 1.0)
    else:
        eq = (0.5, 0.7, 1.0)
    starts = (income, income + low, income + low + med)

    def ret(i: int) -> float:
        w0 = _bucket_start_equity(starts[i], eq[i], low_bucket=(i == 0))
        return round(round(bucket_glide_return(starts[i], w0, a), 3) * 100, 4)

    lad = a.ladder_return
    return [Bucket("Income ladder (FD / debt funds)", income, a.fixed_return, 0, short="Income",
                   ladder_pct=a.fixed_return, start=0),
            Bucket("Low risk", low, ret(0), round(eq[0] * 100), short="Low",
                   ladder_pct=lad, start=starts[0]),
            Bucket("Medium risk", med, ret(1), round(eq[1] * 100), short="Med",
                   ladder_pct=lad, start=starts[1]),
            Bucket("High risk (equity)", high, ret(2), round(eq[2] * 100), short="Equity",
                   ladder_pct=lad, start=starts[2])]


def bucket_corpus(needs: list[float], buckets: list[Bucket]) -> list[float]:
    """Investment required today (at retirement) for each bucket: a fixed
    present-value sum, not a cash-flow projection ('bucket backend'!A1).
    Each year's need is laddered inside its bucket at the bucket's ladder
    return, then the bucket is discounted at its own growth return until it
    starts (low stress buckets M6 = ΣL/(1+B13)^j; R5 = ΣQ/(1+5%)^o,
    R6 = R5/(1+F17)^MAX(K10:K39); W6, AB6 likewise)."""
    out = []
    for b in buckets:
        seg = needs[b.start:b.start + b.years]
        pv = sum(max(0.0, n) / (1 + b.ladder_pct / 100) ** o for o, n in enumerate(seg))
        out.append(pv / (1 + b.return_pct / 100) ** b.start)
    return out


def simulate_drawdown(start_corpus: float, needs: list[float],
                      buckets: list[Bucket]) -> tuple[list[dict], int | None]:
    """Year-by-year projection of the bucket ladder, as the workbook's
    'bucket backend' sheet illustrates it ("not a cash flow projection; meant
    to understand how the buckets work"). Informational only: the corpus and
    SIP come from bucket_corpus, never from this simulation.

    The corpus is split once at retirement: each bucket gets its
    bucket_corpus amount in ladder order and any excess goes to the last
    (high-risk) bucket. No refilling or rebalancing afterwards. Each year the
    need is withdrawn at the start of the year from the earliest bucket that
    still holds money; then a bucket grows at its glide return until the
    year it starts paying and at its ladder return from then on
    ('bucket backend' E/G/I growth columns, D/F/H/J usage columns).
    Returns (rows, index of the first year with a shortfall or None)."""
    targets = bucket_corpus(needs, buckets)
    bal, left = [], max(0.0, start_corpus)
    for t in targets:
        take = min(left, t)
        bal.append(take)
        left -= take
    if bal:
        bal[-1] += left
    rows, depleted = [], None
    for t, need in enumerate(needs):
        opening, start_bal = sum(bal), list(bal)
        due = need
        for i in range(len(bal)):
            take = min(bal[i], due)
            bal[i] -= take
            due -= take
        withdrawal = need - due
        after = list(bal)
        growth = [x * (b.return_pct if t + 1 <= b.start else b.ladder_pct) / 100
                  for x, b in zip(bal, buckets)]
        bal = [x + g for x, g in zip(bal, growth)]
        if due > 1 and depleted is None:
            depleted = t
        rows.append({"year": t + 1, "opening": opening, "balances": start_bal,
                     "balances_after_withdrawal": after, "growth": growth,
                     "need": need, "withdrawal": withdrawal, "shortfall": due if due > 1 else 0.0,
                     "closing": sum(bal)})
    return rows, depleted


@dataclass
class RetirementResult:
    years_to_retirement: float
    first_year_monthly_expense: float
    gross_corpus: float
    corpus_in_hand: float
    net_corpus: float
    monthly_sip: float
    step_up_pct: float
    post_ret_equity_pct: int
    emergency_corpus: float
    on_track: bool
    schedule: list = field(default_factory=list)
    drawdown_corpus: float = 0
    annuity_cost: float = 0
    floor_pct: float = 0
    floor_monthly: float = 0
    income_monthly_at_retirement: float = 0          # streams paying in year 0
    income_streams: list = field(default_factory=list)  # [{monthly_at_retirement, escalation_pct, start, stop}]
    buckets: list = field(default_factory=list)      # [{name, years, return_pct, equity_pct, allocation}]
    cash_flow: list = field(default_factory=list)    # year-by-year drawdown of the funded plan
    depletion_age: int | None = None                 # in cash_flow
    current_path_depletion_age: int | None = None    # with today's investments only
    lumpsum_now: float = 0                           # one-time investment today instead of the SIP
    pre_goal_return: float = 0.0                     # % return assumed while accumulating
    pre_ret_glide: list = field(default_factory=list)  # % return per accumulation year (Step 3 L column)


def retirement_plan(*, current_age: int, retire_age: int, monthly_expense: float,
                    annual_expense: float = 0.0, current_equity: float = 0.0,
                    current_taxable_fi: float = 0.0, current_taxfree_fi: float = 0.0,
                    lumpsum_at_retirement: float = 0.0, step_up_pct: float = 5.0,
                    income_streams=(), floor_pct: float = 0.0,
                    a: Assumptions | None = None) -> RetirementResult:
    """Steps 2–3 + 6: inflation-protected withdrawals to life expectancy funded
    by the workbook's 4-bucket ladder.

    income_streams: up to 3 (monthly amount today, annual escalation %[, start,
      stop]) — rent, pension, other (Step 2 rows 31–41). Each grows at its
      escalation % from today to retirement only (Step 2 C32 = B32·(1+B33)^B7,
      B7 = years to retirement); it then pays that amount in its start year and
      escalates once a year after that (Step 6B AE column: = C32 when the
      year equals the start year F32, else previous × (1+B33)), so a deferred
      stream does not escalate while it waits. It is netted off the year's
      expense only in retirement years start <= k < stop (0-based; start
      None = 0, stop None = never stops).
    Planning period (Settings!C10): life_expectancy − max(current_age,
      retire_age), plus the spouse age gap when a.spouse_age is set (life
      expectancy is the younger spouse's; the plan must last the gap longer).
    floor_pct: income flooring (Step 3 B9). A flat, non-indexed pension for life
      covering this % of the first-year shortfall (expense − income), bought
      at retirement: L51 = (year-1 expense − incomes)·B9, priced as
      F12 = L51 / Settings!H7 (annuity_rate, 4%). The same flat amount is
      netted off every year's need, so the bucket ladder funds the rest,
      including the growth of expenses above the flat pension.
    Corpus (low stress buckets / income flooring F9) = bucket_corpus (PV sum,
      G25) + annuity price (F12) + emergency F13 = 5% × MAX(F10, G25), where
      F10 is the investments in hand at retirement. The cash-flow table is an
      informational simulation of that corpus; it never sizes the corpus.
    SIP: annual schedule (Step 3 N3) at the pre-retirement return.
    """
    a = a or Assumptions()
    streams = []
    for s in income_streams:
        m, e, start, stop = (tuple(s) + (None, None))[:4]
        streams.append((float(m or 0), float(e or 0), int(start or 0),
                        int(stop) if stop is not None else None))
    if len(streams) > 3:
        raise ValueError("At most 3 post-retirement income streams")
    yrs = max(0.0, float(retire_age - current_age))
    avg_monthly = (12 * monthly_expense + annual_expense) / 12
    # Workbook: inflate at pre-retirement inflation (Settings!C3) up to retirement,
    # then at retirement inflation (Settings!C5) through drawdown.
    first_year_monthly = avg_monthly * (1 + a.inflation / 100) ** yrs
    n = planning_period(current_age, retire_age, a)
    infl = a.inflation_retirement / 100
    exp1 = first_year_monthly * 12
    stream_ret = [(m * 12 * (1 + e / 100) ** yrs, e / 100, start, stop)
                  for m, e, start, stop in streams]

    def active(k: int, start: int, stop: int | None) -> bool:
        return start <= k and (stop is None or k < stop)

    def income_in(k: int) -> float:
        return sum(s * (1 + g) ** (k - start)
                   for s, g, start, stop in stream_ret if active(k, start, stop))

    # Income flooring L51: a FLAT nominal amount, not inflation-indexed.
    pct = max(0.0, min(100.0, float(floor_pct))) / 100
    floor1 = max(0.0, exp1 - income_in(0)) * pct if n else 0.0

    years = []
    for k in range(n):
        expense = exp1 * (1 + infl) ** k
        income = income_in(k)
        income_applied = min(income, max(0.0, expense - floor1))
        years.append({"age": retire_age + k, "expense": expense, "floor": floor1,
                      "income": income_applied, "need": max(0.0, expense - floor1 - income)})
    needs = [y["need"] for y in years]
    buckets = bucket_ladder(n, a)
    alloc = bucket_corpus(needs, buckets)
    drawdown = round(sum(alloc))
    annuity = round(floor1 / (a.annuity_rate / 100)) if floor1 else 0
    corpus_in_hand = (fv(current_equity, a.equity_return, yrs)
                      + fv(current_taxable_fi, a.fixed_return, yrs)
                      + fv(current_taxfree_fi, a.taxfree_return, yrs)
                      + lumpsum_at_retirement)
    emergency = round(0.05 * max(corpus_in_hand, drawdown))   # F13
    gross = drawdown + annuity + emergency
    net = max(0.0, gross - corpus_in_hand)
    # Equity share of the ladder (low stress buckets D41 = Σ mix × investment / G25),
    # needed both for the result and as the pre-retirement glide's end weight.
    # A percent number: Bucket.equity_pct is 0–100.
    total_alloc = sum(alloc)
    post_eq_pct = (sum(x * b.equity_pct for x, b in zip(alloc, buckets)) / total_alloc
                   if total_alloc else 0)
    post_eq = round(post_eq_pct)
    # Pre-retirement glide (Step 3 H column, Settings E25): hold 60% equity,
    # then decline in two steps to 20% when retiring at 60 or later, else to
    # the post-retirement equity share; a plan under 10 years out starts at
    # the end weight (E25 = IF(C25<10, G25, 60%)). Fixed income earns C8
    # (the full 7%, not the taxable C8−1% the corpus growth uses).
    end_w = 0.20 if retire_age >= 60 else post_eq_pct / 100
    start_w = end_w if yrs < 10 else 0.60
    glide = _glide_weights(int(round(yrs)), start_w, end_w)
    rates = [_blended(w, a.equity_return, a.taxfree_return) / 100 for w in glide]
    months = int(round(yrs) * 12)
    sip, schedule = solve_sip(net, 0.0, months, step_up_pct, rates=rates) if months > 0 else (0.0, [])
    growth = 1.0
    for r in rates:
        growth *= 1 + r
    lumpsum = net / growth if months > 0 else net

    def run(start: float) -> tuple[list[dict], int | None]:
        rows, dep = simulate_drawdown(start, needs, buckets)
        for row, y in zip(rows, years):
            row.update(y)
        return rows, (retire_age + dep if dep is not None else None)

    spare = corpus_in_hand - emergency - annuity
    cash_flow, depletion_age = run(max(sum(alloc), spare))
    _, current_dep = run(max(0.0, spare))
    # Single-rate summary of the glide: the compounded annual rate it implies.
    pre_ret = (growth ** (1 / len(rates)) - 1) * 100 if rates else 0.0
    return RetirementResult(years_to_retirement=round(yrs),
                            first_year_monthly_expense=round(first_year_monthly),
                            gross_corpus=gross, corpus_in_hand=round(corpus_in_hand),
                            net_corpus=round(net), monthly_sip=sip, step_up_pct=step_up_pct,
                            post_ret_equity_pct=post_eq, emergency_corpus=emergency,
                            on_track=net <= 0, schedule=schedule,
                            pre_ret_glide=[r * 100 for r in rates],
                            drawdown_corpus=drawdown, annuity_cost=annuity,
                            floor_pct=float(floor_pct), floor_monthly=round(floor1 / 12),
                            income_monthly_at_retirement=round(sum(
                                s for s, _, start, stop in stream_ret if active(0, start, stop)) / 12),
                            income_streams=[{"monthly_at_retirement": round(s / 12),
                                             "escalation_pct": g * 100, "start": start, "stop": stop}
                                            for s, g, start, stop in stream_ret],
                            buckets=[{"name": b.name, "short": b.short, "years": b.years, "return_pct": b.return_pct,
                                      "equity_pct": b.equity_pct, "allocation": round(x)}
                                     for b, x in zip(buckets, alloc)],
                            cash_flow=cash_flow, depletion_age=depletion_age,
                            current_path_depletion_age=current_dep,
                            lumpsum_now=round(max(0.0, lumpsum)), pre_goal_return=pre_ret)


def sip_affordability(monthly_sip: float, monthly_take_home: float | None) -> dict | None:
    """Step 2 A24: the suggested SIP as a share of monthly take-home pay (incl.
    employer + employee EPF/NPS). None when take-home is unknown.
    Bands: < 25% comfortable · 25–40% stretch · > 40% aggressive."""
    if not monthly_take_home or float(monthly_take_home) <= 0:
        return None
    # Band on the displayed (rounded) percentage so the text never contradicts it.
    pct = _xround(float(monthly_sip) / float(monthly_take_home) * 100)
    if pct < 25:
        band, note = "comfortable", "comfortably affordable"
    elif pct <= 40:
        band, note = "stretch", "stretch but doable with the step-up starting small"
    else:
        band, note = "aggressive", "aggressive — consider a longer horizon or a lower expense target"
    return {"pct": pct, "band": band, "note": note}


def young_earner_advice(*, current_age: int, monthly_expense: float, annual_expense: float = 0.0,
                        years_to_retirement: float, net_corpus: float,
                        monthly_take_home: float | None = None,
                        a: Assumptions | None = None) -> dict:
    """Step 1's under-26 path, simplified: build a 6-month emergency fund first,
    then a 100% equity SIP stepped up 10% a year toward the retirement gap. No
    bucket ladder — revisit the full plan at 26."""
    a = a or Assumptions()
    emergency = round(6 * (monthly_expense + annual_expense / 12))
    months = int(round(max(0.0, years_to_retirement)) * 12)
    sip = solve_sip(net_corpus, a.equity_return, months, 10)[0] if months > 0 and net_corpus > 0 else 0.0
    return {"emergency_fund": emergency, "monthly_sip": sip, "step_up_pct": 10,
            "equity_pct": 100, "revisit_in_years": max(0, 26 - int(current_age)),
            "affordability": sip_affordability(sip, monthly_take_home)}


# ------------------------------------------------------- goal detail context

def assumption_notes(a: Assumptions, *, kind: str = "one_time",
                     goal_inflation: float | None = None) -> list[dict]:
    """Plain-language notes on the return and inflation assumptions: what
    "post-tax" means in India today, plus realism warnings when the numbers are
    more optimistic than Freefincal's defaults. Returns [{tone, text}] with
    tone "info" or "warn"."""
    notes = [
        {"tone": "info", "text": (
            f"Returns are after tax. Equity at {a.equity_return:g}% assumes long-term gains taxed "
            "at 12.5% above the ₹1.25 lakh yearly exemption. Taxable fixed income "
            f"(FDs, debt funds) at {a.fixed_return:g}% is already net of slab-rate tax: debt fund "
            "gains bought after 1 April 2023 get no indexation. "
            f"PPF/EPF at {a.taxfree_return:g}% is tax-free.")},
    ]
    if a.equity_return > 12:
        notes.append({"tone": "warn", "text": (
            f"Equity at {a.equity_return:g}% after tax is optimistic. Freefincal plans with 10%: "
            "lower it, or the SIP shown here will be too small.")})
    infl = a.inflation if kind == "retirement" else (goal_inflation or a.goal_inflation)
    if a.equity_return - infl > 5:
        notes.append({"tone": "warn", "text": (
            f"Equity beats inflation by {a.equity_return - infl:g} points a year here. "
            "Real returns above about 5% rarely hold over long periods.")})
    if a.fixed_return > a.taxfree_return:
        notes.append({"tone": "warn", "text": (
            f"Taxable fixed income ({a.fixed_return:g}%) beats PPF/EPF ({a.taxfree_return:g}%) "
            "after tax. That is unusual, so check whether it is a pre-tax rate.")})
    if kind != "retirement" and goal_inflation is not None and goal_inflation < a.inflation:
        notes.append({"tone": "warn", "text": (
            f"Goal inflation of {goal_inflation:g}% is below general inflation ({a.inflation:g}%). "
            "Education, healthcare and housing usually rise faster than general inflation, not slower.")})
    return notes


def recent_monthly_investing(buys, today=None, months: int = 6) -> float | None:
    """Average monthly purchase amount over the last `months` months, a proxy
    for "current SIP" across all tracked accounts. `buys` is an iterable of
    (date, amount). None when nothing was bought in the window."""
    import datetime as _dt
    today = today or _dt.date.today()
    since = today - _dt.timedelta(days=round(months * 30.44))
    total = sum(float(amt or 0) for d, amt in buys if since < d <= today)
    return round(total / months) if total > 0 else None


def _sip_path(first_month: float, rates: list[float], start_year: int, total_years: int,
              step_up_pct: float, due: bool = True) -> list[float]:
    """Year-end SIP balances from today (index 0 = today) to total_years, on
    solve_sip's annual schedule. The SIP starts at start_year and steps up
    every SIP year; rates (annual fractions) are indexed by SIP year."""
    path, bal = [0.0], 0.0
    for t in range(total_years):
        k = t - start_year
        if k >= 0:
            c = 12 * first_month * (1 + step_up_pct / 100) ** k
            bal = (bal + c) * (1 + rates[k]) if due else bal * (1 + rates[k]) + c
        path.append(bal)
    return path


def goal_progress(plan, result, a: Assumptions | None = None, *,
                  current_sip: float | None = None, start_year: int | None = None) -> dict:
    """Everything the goal detail page needs to answer "am I on track?".

    plan: a GoalPlan (duck-typed, any object with the same attributes).
    result: the GoalResult / RetirementResult computed for it.
    a: the assumptions used for the result (saved settings, with the retirement
       inflation override applied).
    current_sip: what the user invests per month today (None = unknown; the
       projection then uses existing investments only).

    Returns target, projected corpus at the goal date, surplus (+) or
    shortfall (-), required vs current SIP, an assumptions table, notes and a
    yearly chart series (required path, current path, target).
    """
    import datetime as _dt
    import json as _json
    a = a or Assumptions()
    kind = str(plan.kind)
    eq, tfi, tff = (float(plan.current_equity or 0), float(plan.current_taxable_fi or 0),
                    float(plan.current_taxfree_fi or 0))
    step = float(result.step_up_pct)
    infl_override = float(plan.inflation_override) if getattr(plan, "inflation_override", None) else None
    if kind == "retirement":
        years = max(0, int((plan.retire_age or 0) - (plan.current_age or 0)))
        start, derisk, target = 0.0, 0.0, float(result.gross_corpus)
        end_lumpsum = float(plan.lumpsum_expected or 0)
        ret = result.pre_goal_return
        labels_base, label_fmt = int(plan.current_age or 0), "Age {}"
        goal_label = f"retirement at {plan.retire_age}"
        infl_text = f"{a.inflation:g}% now, {a.inflation_retirement:g}% in retirement"
        post_text = (f"{min(b['return_pct'] for b in result.buckets):g}–"
                     f"{max(b['return_pct'] for b in result.buckets):g}% across the bucket ladder"
                     if result.buckets else "—")
        goal_infl = None
    else:
        years = max(0, int(round(result.years_to_goal)))
        start, derisk, target = result.years_to_start, result.derisk_years, float(result.future_cost)
        end_lumpsum = float(plan.lumpsum_expected or 0) if kind == "one_time" else 0.0
        ret = result.pre_goal_return
        labels_base = start_year or _dt.date.today().year
        label_fmt = "{}"
        goal_label = (f"the goal in {labels_base + years}" if kind == "one_time"
                      else f"the first payment in {labels_base + years}")
        goal_infl = infl_override or a.goal_inflation
        infl_text = f"{goal_infl:g}% a year on the goal cost"
        if kind == "recurring":
            post_text = (f"{result.post_goal_return:g}% in an RD / liquid fund, "
                         "one sinking fund per payment")
        else:
            post_text = "Not needed: a one-time goal is spent on the goal date"
    sip_y = max(0, int(round(years - start))) if years > start else 0
    derisk_m = min(sip_y * 12, int(round(derisk * 12)))
    year_returns = list(getattr(result, "year_returns", None) or [])
    if year_returns and len(year_returns) == sip_y:  # one-time glide path
        rates = _annual_rates(year_returns, derisk_m, a.fixed_return)
    else:
        rates = _annual_rates([ret] * sip_y, derisk_m, a.fixed_return)
    start_y = years - sip_y
    due = getattr(result, "sip_due", True)

    # One-time goals (workbook B19): earmarked money grows only from the SIP
    # start; retirement and recurring goals have start = 0, so from today.
    grow = [max(0.0, t - start) for t in range(years + 1)]
    window = max(0.0, years - start)
    if kind == "retirement" or not derisk:
        existing = [_fv_existing(eq, tfi, tff, g, a) for g in grow]
    else:  # equity switches to FI only in the final `derisk` years
        d = derisk
        existing = [fv(fv(eq, a.equity_return, min(g, window - d)), a.fixed_return,
                       max(0.0, g - (window - d)))
                    + fv(tfi, a.fixed_return, g) + fv(tff, a.taxfree_return, g)
                    for g in grow]
    req_sip = float(result.monthly_sip or 0)
    req = _sip_path(req_sip, rates, start_y, years, step, due) if years else [0.0]
    cur = (_sip_path(float(current_sip), rates, start_y, years, step, due)
           if years and current_sip else [0.0] * (years + 1))
    required_path = [e + s for e, s in zip(existing, req)]
    current_path = [e + s for e, s in zip(existing, cur)]
    required_path[-1] += end_lumpsum
    current_path[-1] += end_lumpsum
    if req_sip == 0 and result.net_corpus > 0:
        # No SIP window: the required path is the lumpsum-today alternative.
        lump = float(result.lumpsum_now)
        g = [lumpsum_growth(ret, round(t * 12)) for t in grow]
        required_path = [c + lump * gg for c, gg in zip(current_path, g)]
    projected = current_path[-1]
    gap = projected - target
    on_track = result.net_corpus <= 0 or gap >= 0
    funded_pct = 100 if target <= 0 else max(0, min(100, round(projected / target * 100)))

    assumptions = [
        ("Inflation", infl_text),
        ("Equity return, after tax", f"{a.equity_return:g}%"),
        ("Fixed income return, after tax", f"{a.fixed_return:g}% taxable · {a.taxfree_return:g}% PPF/EPF"),
        ("Expected return before the goal", f"{ret:g}% a year"
         + (f" on average, {glide_text(result)} along the glide path"
            if year_returns and len(set(year_returns)) > 1 else "")
         + (f", all fixed income ({a.fixed_return:g}%) for the last {derisk:g} years" if derisk else "")),
        ("Return after the goal", post_text),
        ("Years left", f"{years}" + (
            f" (SIP starts in {start:g}; money already set aside is grown from then)"
            if start and kind == "one_time" else f" (SIP starts in {start:g})" if start else "")),
        ("SIP step-up", f"{step:g}% a year"),
    ]
    chart = {"labels": [label_fmt.format(labels_base + t) for t in range(years + 1)],
             "required": [round(v) for v in required_path],
             "current": [round(v) for v in current_path],
             "target": round(target),
             "has_current_sip": bool(current_sip)}
    return {
        "target": round(target), "projected": round(projected), "gap": round(gap),
        "gap_abs": round(abs(gap)), "on_track": on_track, "funded_pct": funded_pct,
        "required_sip": round(req_sip), "current_sip": round(current_sip) if current_sip else None,
        "sip_gap": round(float(current_sip) - req_sip) if current_sip else None,
        "lumpsum_now": round(float(result.lumpsum_now or 0)),
        "years_left": years, "goal_label": goal_label,
        "assumptions": assumptions,
        "notes": assumption_notes(a, kind=kind, goal_inflation=goal_infl),
        "chart": _json.dumps(chart),
    }
