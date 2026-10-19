"""Goal planning engine — a faithful port of the Freefincal robo-advisor workbook
(July 2025) goal math: one-time goals, recurring goals, and the low-stress
retirement estimate. Pure functions; no Django dependencies.

Workbook conventions preserved:
- Existing corpus grows at its own post-tax rate (equity / taxable FI / tax-free FI);
  for one-time goals only from the SIP start year to the goal (Step 4A B19).
- Goal cost inflates at education-style inflation (default 10%, overridable).
- Net corpus = inflated cost − FV of earmarked investments − expected lumpsums.
- Monthly SIP solves for net corpus with an annual step-up (default 5%); one-time
  goals earn a glide-path return that de-risks year by year toward the goal
  (Settings rows 45–48).
- Retirement: expenses that persist inflate at 6% to retirement; corpus must
  fund inflation-protected withdrawals until life expectancy through the
  workbook's 4-bucket ladder (income / low / medium / high risk), each bucket
  earning its own return and refilled from the equity end every year.
- Post-retirement income (rent, pension…) and an optional annuity floor reduce
  what the investment corpus must fund.
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


def _year_rates(year_returns_pct: list[float], derisk_months: int = 0,
                derisk_return_pct: float | None = None) -> list[float]:
    """Per-month rates from one annual return per year (return/12 each month),
    with the final derisk_months at the fixed-income return."""
    rates = [r / 100 / 12 for r in year_returns_pct for _ in range(12)]
    if derisk_months and derisk_return_pct is not None:
        cut = max(0, len(rates) - int(derisk_months))
        rates = rates[:cut] + [derisk_return_pct / 100 / 12] * (len(rates) - cut)
    return rates


def _monthly_rates(annual_return_pct: float, months: int, derisk_months: int = 0,
                   derisk_return_pct: float | None = None) -> list[float]:
    """Per-month rates: the goal portfolio's return, then (de-risking) the
    fixed-income return for the final derisk_months."""
    r = annual_return_pct / 100 / 12
    if not derisk_months or derisk_return_pct is None:
        return [r] * months
    r2 = derisk_return_pct / 100 / 12
    cut = max(0, months - int(derisk_months))
    return [r if i < cut else r2 for i in range(months)]


def lumpsum_growth(annual_return_pct: float, months: int, derisk_months: int = 0,
                   derisk_return_pct: float | None = None) -> float:
    """Growth factor of ₹1 invested now over `months`, monthly compounding,
    with an optional fixed-income tail (same convention as solve_sip)."""
    g = 1.0
    for r in _monthly_rates(annual_return_pct, months, derisk_months, derisk_return_pct):
        g *= 1 + r
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
              rates: list[float] | None = None) -> tuple[float, list]:
    """Monthly SIP that grows step_up% each year and reaches net_corpus.
    Binary-searched on a month-by-month simulation (mirrors the workbook's
    investment schedule). With derisk_months, the portfolio earns
    derisk_return_pct (fixed income) for the last derisk_months instead.
    rates (one per month) overrides the single return, e.g. a glide path."""
    if months <= 0:
        return 0.0, []
    if rates is None:
        rates = _monthly_rates(annual_return_pct, months, derisk_months, derisk_return_pct)

    def end_value(first_month: float) -> float:
        bal, m = 0.0, first_month
        for i in range(months):
            if i and i % 12 == 0:
                m *= 1 + step_up_pct / 100
            bal = bal * (1 + rates[i]) + m
        return bal

    lo, hi = 0.0, max(net_corpus, 1.0)
    for _ in range(80):
        mid = (lo + hi) / 2
        if end_value(mid) < net_corpus:
            lo = mid
        else:
            hi = mid
    sip = (lo + hi) / 2
    # year-end schedule
    schedule, bal, m = [], 0.0, sip
    for i in range(months):
        if i and i % 12 == 0:
            m *= 1 + step_up_pct / 100
        bal = bal * (1 + rates[i]) + m
        if (i + 1) % 12 == 0 or i == months - 1:
            schedule.append(((i + 1) // 12 if (i + 1) % 12 == 0 else None, m, round(bal)))
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
    rates = _year_rates(year_returns, derisk_m, a.fixed_return)
    schedule: list = []
    if months > 0:
        sip, schedule = solve_sip(net, 0.0, months, step_up_pct, rates=rates)
        lumpsum = net / math.prod(1 + r for r in rates)
        # Average glide-path return (de-risking excluded, as before): the single
        # annual rate that, compounded monthly, grows ₹1 as much as the path.
        glide = math.prod(1 + r for r in _year_rates(year_returns))
        ret = round((glide ** (1 / months) - 1) * 1200, 4)
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
                   a: Assumptions | None = None) -> GoalResult:
    """Step 4B: recurring goals are funded with a sinking fund, not an equity
    corpus.

    The workbook is explicit: "for all these goals, you use a simple RD or
    sweep in SB acct or liquid fund". So the money earns the fixed-income
    (liquid / RD) rate both while saving and while spending, and the corpus is
    the PV of the cost stream discounted at that same rate. Costs are paid at
    the start of each year (fees, premiums), so the first payment is due on the
    goal date: PV is an annuity-due. post_goal_return overrides the spend rate
    (None = the same liquid rate). derisk_years and lumpsum_now behave as in
    one_time_goal, though de-risking is a no-op once everything is already in
    fixed income."""
    a = a or Assumptions()
    infl = (inflation_override if inflation_override else a.goal_inflation) / 100
    ret_pct = a.fixed_return
    post_pct = ret_pct if post_goal_return is None else float(post_goal_return)
    c1 = annual_cost_today * (1 + infl) ** years_until_start
    n, g, r = int(round(years_duration)), infl, post_pct / 100
    if abs(r - g) > 1e-9:
        pv = c1 * (1 - ((1 + g) / (1 + r)) ** n) / (r - g) * (1 + r)
    else:
        pv = c1 * n
    pv = max(0.0, pv)
    y = max(0.0, float(years_until_start))
    d = min(max(0.0, float(derisk_years)), y)
    fv_current = _fv_existing(current_equity, current_taxable_fi, current_taxfree_fi, y, a, d)
    net = max(0.0, pv - fv_current)
    months = int(round(y) * 12)
    derisk_m = int(round(d * 12))
    if months > 0:
        sip, schedule = solve_sip(net, ret_pct, months, step_up_pct, derisk_m, a.fixed_return)
        lumpsum = net / lumpsum_growth(ret_pct, months, derisk_m, a.fixed_return)
    else:
        sip, schedule, lumpsum = 0.0, [], net
    return GoalResult(future_cost=round(pv), fv_current=round(fv_current),
                      net_corpus=round(net), lumpsum_now=round(max(0.0, lumpsum)),
                      monthly_sip=sip, step_up_pct=step_up_pct,
                      equity_alloc_pct=0, schedule=schedule,
                      pre_goal_return=ret_pct, post_goal_return=post_pct, derisk_years=d,
                      years_to_goal=y, years_to_start=0.0)


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
                             life_expectancy=a.life_expectancy, spouse_age=a.spouse_age), kwargs),
        ("Base", a, kwargs),
        ("Bull", Assumptions(inflation=5, inflation_retirement=5, equity_return=12,
                             fixed_return=7, taxfree_return=7.5, goal_inflation=8,
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
                note = "spent from a 25%-equity mix" + (
                    f", fixed income for the last {r.derisk_years:g} years" if r.derisk_years else "")
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
    return_pct: float
    equity_pct: int
    short: str = ""


def bucket_ladder(period: int, a: Assumptions | None = None) -> list[Bucket]:
    """The workbook's 4-bucket ladder (Settings!E13:E16 tenures, C19:C22 mixes).

    Income bucket: first min(T, 15) years, a fixed-income ladder (workbook 6% ≈
    taxable FI). Then Low / Medium / High risk buckets whose tenures and equity
    shares depend on the total period T; each blends equity with the workbook's
    all-FI return (Settings!C8 = tax-free FI rate)."""
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

    def ret(w: float) -> float:
        return round(w * a.equity_return + (1 - w) * a.taxfree_return, 4)

    return [Bucket("Income ladder (FD / debt funds)", income, a.fixed_return, 0, short="Income"),
            Bucket("Low risk", low, ret(eq[0]), round(eq[0] * 100), short="Low"),
            Bucket("Medium risk", med, ret(eq[1]), round(eq[1] * 100), short="Med"),
            Bucket("High risk (equity)", high, ret(eq[2]), round(eq[2] * 100), short="Equity")]


def _allocate(total: float, remaining_needs: list[float], buckets: list[Bucket]) -> list[float]:
    """Refill the ladder: each near bucket gets the PV (at its own return) of the
    years it now covers; the high-risk bucket holds whatever is left. If money
    is short, nearer buckets are filled first."""
    out, start, left = [], 0, total
    for b in buckets[:-1]:
        r = b.return_pct / 100
        target = sum(n / (1 + r) ** k
                     for k, n in enumerate(remaining_needs[start:start + b.years], start=start))
        take = min(left, target)
        out.append(take)
        left -= take
        start += b.years
    out.append(left)
    return out


def simulate_drawdown(start_corpus: float, needs: list[float],
                      buckets: list[Bucket]) -> tuple[list[dict], int | None]:
    """Year-by-year bucket drawdown (Step 6 style). Each year: rebalance the
    ladder forward, withdraw the year's need (start of year) from the income
    bucket first, then grow every bucket at its own return.
    Returns (rows, index of the first year with a shortfall or None)."""
    rows, total, depleted = [], max(0.0, start_corpus), None
    for t, need in enumerate(needs):
        bal = _allocate(total, needs[t:], buckets)
        opening, start_bal = total, list(bal)
        due = need
        for i in range(len(bal)):
            take = min(bal[i], due)
            bal[i] -= take
            due -= take
        withdrawal = need - due
        growth = [x * b.return_pct / 100 for x, b in zip(bal, buckets)]
        total = sum(bal) + sum(growth)
        if due > 1 and depleted is None:
            depleted = t
        rows.append({"year": t + 1, "opening": opening, "balances": start_bal,
                     "balances_after_withdrawal": list(bal), "growth": growth,
                     "need": need, "withdrawal": withdrawal, "shortfall": due if due > 1 else 0.0,
                     "closing": total})
    return rows, depleted


def required_corpus(needs: list[float], buckets: list[Bucket]) -> float:
    """Smallest starting corpus that meets every year's need via the ladder."""
    if not needs or sum(needs) <= 0:
        return 0.0
    lo, hi = 0.0, sum(needs)
    while simulate_drawdown(hi, needs, buckets)[1] is not None:
        hi *= 2
    for _ in range(60):
        mid = (lo + hi) / 2
        if simulate_drawdown(mid, needs, buckets)[1] is None:
            hi = mid
        else:
            lo = mid
    return hi


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
    floor_pct: an inflation-indexed annuity bought at retirement covering this %
      of the first-year expense, rising with retirement inflation for life. Its
      price (PV at the taxable-FI rate) is added to the corpus; the bucket
      ladder funds only the remainder.
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
    floor1 = exp1 * max(0.0, min(100.0, float(floor_pct))) / 100

    def active(k: int, start: int, stop: int | None) -> bool:
        return start <= k and (stop is None or k < stop)

    years = []
    for k in range(n):
        expense = exp1 * (1 + infl) ** k
        floor = floor1 * (1 + infl) ** k
        income = sum(s * (1 + g) ** (k - start)
                     for s, g, start, stop in stream_ret if active(k, start, stop))
        income_applied = min(income, max(0.0, expense - floor))
        years.append({"age": retire_age + k, "expense": expense, "floor": floor,
                      "income": income_applied, "need": max(0.0, expense - floor - income)})
    needs = [y["need"] for y in years]
    buckets = bucket_ladder(n, a)
    drawdown = round(required_corpus(needs, buckets))
    annuity = round(sum(y["floor"] / (1 + a.fixed_return / 100) ** k for k, y in enumerate(years)))
    emergency = round(first_year_monthly * 6)
    gross = drawdown + annuity + emergency
    corpus_in_hand = (fv(current_equity, a.equity_return, yrs)
                      + fv(current_taxable_fi, a.fixed_return, yrs)
                      + fv(current_taxfree_fi, a.taxfree_return, yrs)
                      + lumpsum_at_retirement)
    net = max(0.0, gross - corpus_in_hand)
    months = int(round(yrs) * 12)
    pre_ret = _blended(0.65, a.equity_return, a.fixed_return)
    sip, schedule = solve_sip(net, pre_ret, months, step_up_pct) if months > 0 else (0.0, [])
    lumpsum = net / lumpsum_growth(pre_ret, months) if months > 0 else net

    def run(start: float) -> tuple[list[dict], int | None]:
        rows, dep = simulate_drawdown(start, needs, buckets)
        for row, y in zip(rows, years):
            row.update(y)
        return rows, (retire_age + dep if dep is not None else None)

    spare = corpus_in_hand - emergency - annuity
    cash_flow, depletion_age = run(max(drawdown, spare))
    _, current_dep = run(max(0.0, spare))
    alloc = cash_flow[0]["balances"] if cash_flow else [0.0] * len(buckets)
    total_alloc = sum(alloc)
    post_eq = round(sum(x * b.equity_pct for x, b in zip(alloc, buckets)) / total_alloc) if total_alloc else 0
    return RetirementResult(years_to_retirement=round(yrs),
                            first_year_monthly_expense=round(first_year_monthly),
                            gross_corpus=gross, corpus_in_hand=round(corpus_in_hand),
                            net_corpus=round(net), monthly_sip=sip, step_up_pct=step_up_pct,
                            post_ret_equity_pct=post_eq, emergency_corpus=emergency,
                            on_track=net <= 0, schedule=schedule,
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


def _sip_path(first_month: float, rates: list[float], start_month: int, total_months: int,
              step_up_pct: float) -> list[float]:
    """Year-end SIP balances from today (index 0 = today) to total_months.
    The SIP starts at start_month and steps up every 12 SIP months; rates are
    indexed by SIP month (as in solve_sip)."""
    path, bal, m, k = [0.0], 0.0, first_month, 0
    for i in range(total_months):
        if i >= start_month:
            if k and k % 12 == 0:
                m *= 1 + step_up_pct / 100
            bal = bal * (1 + rates[k]) + m
            k += 1
        if (i + 1) % 12 == 0 or i == total_months - 1:
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
            post_text = f"{result.post_goal_return:g}% while the corpus is spent"
        else:
            post_text = "Not needed: a one-time goal is spent on the goal date"
    total_m = years * 12
    sip_m = max(0, int(round(years - start)) * 12) if years > start else 0
    derisk_m = min(sip_m, int(round(derisk * 12)))
    year_returns = list(getattr(result, "year_returns", None) or [])
    if year_returns and len(year_returns) * 12 == sip_m:  # one-time glide path
        rates = _year_rates(year_returns, derisk_m, a.fixed_return)
    else:
        rates = _monthly_rates(ret, sip_m, derisk_m, a.fixed_return)
    start_m = total_m - sip_m

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
    req = _sip_path(req_sip, rates, start_m, total_m, step) if total_m else [0.0]
    cur = (_sip_path(float(current_sip), rates, start_m, total_m, step)
           if total_m and current_sip else [0.0] * (years + 1))
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
